import json
import re
import pandas as pd
from tqdm import tqdm
from utils import (
    sample_from_two_lists,
    get_dfs_info,
    get_tool,
    filter_code,
    read_jsonl,
    filter_cot,
    timeout,
    TimeoutException,
    execute_with_timeout,
    load_json,
    save_json,
)
from table_qa_execution_eval.sft_prompt import (
    prompt_with_format_list,
    prompt_with_instruction_list,
)
from inference import (
    generate_outputs,
    load_model,
    load_tokenizer_and_template,
    get_infer_kwargs,
)
import os
import argparse
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from joblib import Parallel, delayed


CODE_PREFIX = """import matplotlib.pyplot as plt
from mplfonts import use_font
import pandas as pd
import numpy as np
import seaborn as sns
import warnings

warnings.filterwarnings("ignore")
# Fixing Chinese font issues
use_font("Noto Serif CJK SC")
plt.rcParams['font.sans-serif']=['SimHei']
plt.rcParams['axes.unicode_minus']=False\n"""


def format_inputs(test_datas: list[dict],args) -> list[list[dict]]:
    """Format inputs to the required messages"""
    # 把需要推理的数据拼成 message 形式
    format_message_datas = []
    for idx, test_dt in enumerate(test_datas):
        if args.slim:
            messages = test_dt["message"]
        else:
            instruction = test_dt["instruction"]
            table_info = test_dt["table_info"]
            df_info_simple_str = test_dt["df_info_simple_str"]
            instruction = instruction.replace(table_info, df_info_simple_str)
            messages = [{"role": "user", "content": instruction}]
        format_message_datas.append(messages)

    return format_message_datas


def eval_outputs_parallel(
    llm_output: str,
    test_data: str,
    args,
) -> dict:
    df_paths = test_data["table_paths"]
    df_names = test_data["df_names"]
    query = test_data["query"]
    table_paths = test_data["table_paths"]
    df = [pd.read_csv(path, low_memory=False) for path in df_paths]

    if args.slim:
        # tool = get_tool(df, df_names)
        tool = get_tool(df) 
        instruction = test_data["message"]
    else:
        tool = get_tool(df, df_names)
        instruction = test_data["instruction"]
        table_info = test_data["table_info"]
        df_info_simple_str = test_data["df_info_simple_str"]
        instruction = instruction.replace(table_info, df_info_simple_str)

    code, _ = filter_code(llm_output)
    # cot = filter_cot(llm_output)
    eval_result_sample = {}
    # 运行超时代码，认为都是异常代码， 在tool.run()过程中，可能会print出额外的内容，不影响执行
    try:
        # 如果生成的代码为空（解析不到代码）， 也认为是llm没有理解observe内容或instruct， 输出为Code Error
        if not code:
            observe = "Code Error: output empty code.."
        elif 'df.explode("Candidate")' in code:
            raise ValueError(f"df.explode error")
        else:
            with timeout(15):  # 设置超时时间为15秒
                pure_code = CODE_PREFIX + code
                # print("pure code:", pure_code)
                observe = tool.run(pure_code)  # 需要监控超时的代码块
                # observe = execute_with_timeout(pure_code, 15, tool)
                if isinstance(observe, pd.DataFrame):
                    observe = observe.head().to_markdown(index=False)
                else:
                    observe = str(observe)
    except TimeoutException as e:
        observe = f"Timeout Error: code running time exceed 15s.."
    except SystemExit as e:
        observe = f"SystemExit Error: {str(e)}"
    except Exception as e:
        observe = f"Unexpected Error: {str(e)}"

    eval_result_sample["code"] = code
    eval_result_sample["llm_output"] = llm_output
    eval_result_sample["observe"] = observe
    eval_result_sample["flag"] = execution_eval(observe)
    eval_result_sample["query"] = query
    eval_result_sample["table_paths"] = table_paths
    eval_result_sample["instruction"] = instruction

    return eval_result_sample


def execution_eval(observe: str) -> bool:
    """
    Test whether the code generated by eval_llm can be executed.
    :param output: output code of llm generation
    :return: True or False
    """
    # 只要执行结果中不出现error 或者 exception， 就认为代码可执行
    pattern = re.compile(r"error|exception", re.IGNORECASE)
    try:
        res = not pattern.search(observe)
    except:
        res = True
    return res


def main(args):
    eval_dataset_path = args.eval_dataset_path
    eval_results_save_path = args.eval_results_save_path
    model_path = args.model_path
    max_model_len = args.max_model_len
    template = args.template
    gpus_num = args.gpus_num
    model_kwargs = get_infer_kwargs(args)
    print("Load model...")
    llm_model = load_model(model_path, max_model_len, gpus_num)
    tokenizer = load_tokenizer_and_template(model_path, template)
    eval_dataset_path = args.eval_dataset_path
    test_datas = load_json(eval_dataset_path)

    format_message_datas = format_inputs(test_datas,args)

    print("Generating eval answers now..")
    model_outputs = generate_outputs(
        format_message_datas, llm_model, tokenizer, model_kwargs
    )
    # with open("model_output.json","w")as f:
    #     json.dump(model_outputs,f,ensure_ascii=False)
    print("Generating answers finished..")


    eval_answers = Parallel(n_jobs=48)(
        delayed(eval_outputs_parallel)(model_outputs[i]["output_text"], test_datas[i],args)
        for i in range(len(test_datas))
    )

    # calculate  execute rate
    execute_passed = 0
    total_len = len(eval_answers)
    for eval_answer in eval_answers:
        execute_passed += int(eval_answer["flag"])
    print(f"Sample length: {total_len}. ")
    print(
        f"Execute Passed: {execute_passed}." f"\tExecute pass-rate is:",
        round(execute_passed / total_len, 3),
    )

    # save eval result
    with open(eval_results_save_path, "w", encoding="utf-8") as f:
        json.dump(eval_answers, f, ensure_ascii=False)


if __name__ == "__main__":
    # 确定images目录是否存在和写权限
    output_dir = Path(__file__).parent / "images"
    if os.path.exists(output_dir):
        if not os.access(output_dir, os.W_OK):
            shutil.rmtree(output_dir)
            os.makedirs(output_dir)
            os.chmod(output_dir, 0o777)
            print("not write permission, makedir:", output_dir)
        else:
            print(f"{output_dir} exists!")
    else:
        os.makedirs(output_dir)
        os.chmod(output_dir, 0o777)
        print("makedir:", output_dir)
    parser = argparse.ArgumentParser(description="eval tableqa python code")
    parser.add_argument(
        "--gpus_num", type=int, default=1, help="the number of GPUs you want to use."
    )
    parser.add_argument(
        "--temperature", type=float, default=0.01, help="Temperature setting"
    )

    parser.add_argument(
        "--template",
        type=str,
        choices=[None, "llama3", "baichuan", "chatglm"],
        default=None,
        help="The template must be specified if not present in the config file",
    )

    parser.add_argument(
        "--model_path", type=str, required=True, help="Path to the model"
    )
    parser.add_argument(
        "--model_type",
        choices=["base_model", "chat_model"],
        default="chat_model",
        help="Base model or Chat model",
    )
    parser.add_argument(
        "--slim",
        action="store_true",
        help="slim data format",
    )
    parser.add_argument(
        "--max_new_tokens",
        type=int,
        default=1024,
        help="Maximum number of output tokens",
    )
    parser.add_argument("--max_model_len", type=int, default=8192, help="Cutoff length")
    parser.add_argument(
        "--eval_dataset_path",
        type=str,
        default="table_related_benchmarks/evalset/table_qa_execuate_test/test_datas_zuizong_filter.json",
        help="Test Set Path",
    )

    parser.add_argument(
        "--eval_results_save_path",
        type=str,
        default="output/result_table_qa.json",
        help="Max iteration for llm to run each code correction task",
    )
    args = parser.parse_args()

    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    main(args)
    """
    python run_eval.py --model_path /data0/pretrained-models/Qwen2-7B-Instruct
    """
