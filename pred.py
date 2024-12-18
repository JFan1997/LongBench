import os
from datasets import load_dataset
import torch
import json
from transformers import (
    AutoTokenizer,
    LlamaTokenizer,
    GemmaTokenizer,
    AutoModelForCausalLM,
    LlamaForCausalLM,
    GemmaForCausalLM,
)
from tqdm import tqdm
import numpy as np
import random
import argparse
from llama_flash_attn_monkey_patch import replace_llama_attn_with_flash_attn
import torch.distributed as dist
import torch.multiprocessing as mp

# Import SelfExtend
import sys

sys.path.insert(0, "../LongLM/")
import SelfExtend


def parse_args(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        choices=[
            "llama2-7b-chat-4k",
            "longchat-v1.5-7b-32k",
            "xgen-7b-8k",
            "internlm-7b-8k",
            "chatglm2-6b",
            "chatglm2-6b-32k",
            "chatglm3-6b-32k",
            "vicuna-v1.5-7b-16k",
            "phi1.5-1b",
            "phi2-3b",
            "gemma-2b",
            "gemma2-2b",
            "llama3.2-1b",
            "llama3.2-3b",
            "pythia-1.4b",
            "phi-3-mini-4k-3.8b",
            "phi-3-mini-128k-3.8b"
        ],
    )
    parser.add_argument("--e", action="store_true", help="Evaluate on LB-E")
    parser.add_argument("--se", action="store_true", help="Apply SelfExtend")
    return parser.parse_args(args)


# This is the customized building prompt for chat models
def build_chat(tokenizer, prompt, model_name):
    if "chatglm3" in model_name:
        prompt = tokenizer.build_chat_input(prompt)
    elif "chatglm" in model_name:
        prompt = tokenizer.build_prompt(prompt)
    elif "longchat" in model_name or "vicuna" in model_name:
        from fastchat.model import get_conversation_template

        conv = get_conversation_template("vicuna")
        conv.append_message(conv.roles[0], prompt)
        conv.append_message(conv.roles[1], None)
        prompt = conv.get_prompt()
    elif "llama" in model_name:  # elif "llama2" in model_name:
        prompt = f"[INST]{prompt}[/INST]"
    elif "xgen" in model_name:
        header = (
            "A chat between a curious human and an artificial intelligence assistant. "
            "The assistant gives helpful, detailed, and polite answers to the human's questions.\n\n"
        )
        prompt = header + f" ### Human: {prompt}\n###"
    elif "internlm" in model_name:
        prompt = f"<|User|>:{prompt}<eoh>\n<|Bot|>:"
    # elif "gemma" in model_name:
    #     prompt = f"<start_of_turn>user\n{prompt}<end_of_turn>\n<start_of_turn>model\n"
    return prompt


def post_process(response, model_name):
    if "xgen" in model_name:
        response = response.strip().replace("Assistant:", "")
    elif "internlm" in model_name:
        response = response.split("<eoa>")[0]
    # elif "gemma" in model_name:
    #     response = response.split("<end_of_turn>")[0]
    return response


def get_pred(
    rank,
    world_size,
    data,
    max_length,
    max_gen,
    prompt_format,
    dataset,
    device,
    model_name,
    model2path,
    out_path,
    apply_se,
):
    device = torch.device(f"cuda:{rank}")
    model, tokenizer = load_model_and_tokenizer(
        model2path[model_name], model_name, device, apply_se
    )
    for json_obj in tqdm(data):
        prompt = prompt_format.format(**json_obj)
        # truncate to fit max_length (we suggest truncate in the middle, since the left and right side may contain crucial instructions)
        tokenized_prompt = tokenizer(
            prompt, truncation=False, return_tensors="pt"
        ).input_ids[0]
        if "chatglm3" in model_name:
            tokenized_prompt = tokenizer(
                prompt, truncation=False, return_tensors="pt", add_special_tokens=False
            ).input_ids[0]
        if len(tokenized_prompt) > max_length:
            half = int(max_length / 2)
            prompt = tokenizer.decode(
                tokenized_prompt[:half], skip_special_tokens=True
            ) + tokenizer.decode(tokenized_prompt[-half:], skip_special_tokens=True)
        if dataset not in [
            "trec",
            "triviaqa",
            "samsum",
            "lsht",
            "lcc",
            "repobench-p",
        ]:  # chat models are better off without build prompts on these tasks
            prompt = build_chat(tokenizer, prompt, model_name)
        if "chatglm3" in model_name:
            if dataset in ["trec", "triviaqa", "samsum", "lsht", "lcc", "repobench-p"]:
                input = tokenizer(prompt, truncation=False, return_tensors="pt").to(
                    device
                )
            else:
                input = prompt.to(device)
        else:
            input = tokenizer(prompt, truncation=False, return_tensors="pt").to(device)
        context_length = input.input_ids.shape[-1]
        if (
            dataset == "samsum"
        ):  # prevent illegal output on samsum (model endlessly repeat "\nDialogue"), might be a prompting issue
            output = model.generate(
                **input,
                max_new_tokens=max_gen,
                num_beams=1,
                do_sample=False,
                temperature=1.0,
                min_length=context_length + 1,
                eos_token_id=[
                    tokenizer.eos_token_id,
                    tokenizer.encode("\n", add_special_tokens=False)[-1],
                ],
            )[0]
        else:
            if "pythia" in model_name:
                output = model.generate(
                    **input,
                    max_new_tokens=max_gen,
                    num_beams=1,
                    do_sample=False,
                    temperature=1.0,
                    pad_token_id=tokenizer.eos_token_id,
                )[0]
            else:
                output = model.generate(
                    **input,
                    max_new_tokens=max_gen,
                    num_beams=1,
                    do_sample=False,
                    temperature=1.0,
                )[0]
        pred = tokenizer.decode(output[context_length:], skip_special_tokens=True)
        pred = post_process(pred, model_name)
        with open(out_path, "a", encoding="utf-8") as f:
            json.dump(
                {
                    "pred": pred,
                    "answers": json_obj["answers"],
                    "all_classes": json_obj["all_classes"],
                    "length": json_obj["length"],
                },
                f,
                ensure_ascii=False,
            )
            f.write("\n")
    if dist.is_initialized():  # NOTE: add this line to avoid assertion error
        dist.destroy_process_group()


def seed_everything(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.cuda.manual_seed_all(seed)


def load_model_and_tokenizer(path, model_name, device, apply_se):
    if (
        "chatglm" in model_name
        or "internlm" in model_name
        or "xgen" in model_name
        or "phi" in model_name
        or "gemma" in model_name
        or "pythia" in model_name
    ):
        tokenizer = AutoTokenizer.from_pretrained(path, trust_remote_code=True)
        model = AutoModelForCausalLM.from_pretrained(
            path,
            trust_remote_code=True,
            torch_dtype=torch.bfloat16,
            # use_flash_attention_2=True,
            # attn_implementation="flash_attention_2",
        ).to(device)
    # elif "gemma" in model_name:
    #     tokenizer = GemmaTokenizer.from_pretrained(
    #         path,
    #         trust_remote_code=True,
    #         torch_dtype=torch.float16,  # NOTE: bfloat16 causes error
    #     )
    #     model = GemmaForCausalLM.from_pretrained(
    #         path,
    #         trust_remote_code=True,
    #         torch_dtype=torch.float16,  # NOTE: bfloat16 causes error
    #     ).to(device)
    elif "llama2" in model_name:
        replace_llama_attn_with_flash_attn()
        tokenizer = LlamaTokenizer.from_pretrained(path)
        model = LlamaForCausalLM.from_pretrained(
            path,
            torch_dtype=torch.bfloat16,
        ).to(device)
    elif "llama3" in model_name:
        replace_llama_attn_with_flash_attn()
        tokenizer = AutoTokenizer.from_pretrained(path)
        model = AutoModelForCausalLM.from_pretrained(
            path,
            torch_dtype=torch.bfloat16,
        ).to(device)
    elif "longchat" in model_name or "vicuna" in model_name:
        from fastchat.model import load_model

        replace_llama_attn_with_flash_attn()
        model, _ = load_model(
            path,
            device="cpu",
            num_gpus=0,
            load_8bit=False,
            cpu_offloading=False,
            debug=False,
        )
        model = model.to(device)
        model = model.bfloat16()
        tokenizer = AutoTokenizer.from_pretrained(
            path, trust_remote_code=True, use_fast=False
        )
    model = model.eval()
    if apply_se:
        # NOTE: Do NOT use flash attention to avoid error
        SelfExtend.apply(
            model,
            group_size=8,
            window_size=1024,
            enable_flash_attention=("llama" in model_name),
            flash_attention_impl="flash_attn",
        )
        print("SelfExtend with flash attention applied to model")
    return model, tokenizer


if __name__ == "__main__":
    seed_everything(42)
    args = parse_args()
    world_size = torch.cuda.device_count()
    mp.set_start_method("spawn", force=True)

    apply_se = args.se

    model2path = json.load(open("config/model2path.json", "r"))
    model2maxlen = json.load(open("config/model2maxlen.json", "r"))
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model_name = args.model
    # define your model
    max_length = model2maxlen[model_name]
    if args.e:
        datasets = [
            "qasper",
            # "multifieldqa_en",
            # "hotpotqa",
            # "2wikimqa",
            # "gov_report",
            # "multi_news",
            # "trec",
            # "triviaqa",
            # "samsum",
            # "passage_count",
            # "passage_retrieval_en",
            # "lcc",
            # "repobench-p",
        ]
    else:
        datasets = [
            "narrativeqa",
            "qasper",
            "multifieldqa_en",
            "multifieldqa_zh",
            "hotpotqa",
            "2wikimqa",
            "musique",
            "dureader",
            "gov_report",
            "qmsum",
            "multi_news",
            "vcsum",
            "trec",
            "triviaqa",
            "samsum",
            "lsht",
            "passage_count",
            "passage_retrieval_en",
            "passage_retrieval_zh",
            "lcc",
            "repobench-p",
        ]
    # we design specific prompt format and max generation length for each task,
    # feel free to modify them to optimize model output
    dataset2prompt = json.load(open("config/dataset2prompt.json", "r"))
    dataset2maxlen = json.load(open("config/dataset2maxlen.json", "r"))
    # predict on each dataset
    se_suffix = ""
    if apply_se:
        se_suffix = "_se"
    if not os.path.exists(f"pred{se_suffix}"):
        os.makedirs(f"pred{se_suffix}")
    if not os.path.exists(f"pred_e{se_suffix}"):
        os.makedirs(f"pred_e{se_suffix}")
    for dataset in datasets:
        if args.e:
            data = load_dataset("THUDM/LongBench", f"{dataset}_e", split="test")
            if not os.path.exists(f"pred_e{se_suffix}/{model_name}"):
                os.makedirs(f"pred_e{se_suffix}/{model_name}")
            out_path = f"pred_e{se_suffix}/{model_name}/{dataset}.jsonl"
        else:
            data = load_dataset("THUDM/LongBench", dataset, split="test")
            if not os.path.exists(f"pred{se_suffix}/{model_name}"):
                os.makedirs(f"pred{se_suffix}/{model_name}")
            out_path = f"pred{se_suffix}/{model_name}/{dataset}.jsonl"
        prompt_format = dataset2prompt[dataset]
        max_gen = dataset2maxlen[dataset]
        data_all = [data_sample for data_sample in data]
        data_subsets = [data_all[i::world_size] for i in range(world_size)]
        processes = []
        for rank in range(world_size):
            p = mp.Process(
                target=get_pred,
                args=(
                    rank,
                    world_size,
                    data_subsets[rank],
                    max_length,
                    max_gen,
                    prompt_format,
                    dataset,
                    device,
                    model_name,
                    model2path,
                    out_path,
                    apply_se,
                ),
            )
            p.start()
            processes.append(p)
        for p in processes:
            p.join()
