OPENAI_API_BASE = "https://api.52099520.xyz/v1"
OPENAI_API_KEY = ""
# OPENAI_API_BASE = "https://api.openai.com/v1"

USE_LANGSMITH = False
LANGCHAIN_API_KEY = None
LANGCHAIN_PROJECT = None
TAVILY_API_KEY = None


SequentialHistoryFile = "history/sequential.json"
HierarchicalHistoryFile = "history/hierarchical.json"


CONFIG_AGENT_Default = {
    # "inc": "openai",
    # "model_name": "gpt-4o",
    "inc": "small_qwen",
    "model_name": "qwen-flash",
    # "model_name": "gpt-4-1106-preview",
    # "model_name": "gpt-3.5-turbo-0125",
}

CONFIG_AGENT_ExceedLength = {
    "inc": "openai",
    "model_name": "gpt-5",
}

CONFIG_AGENT_Retry = {
    "inc": "openai",
    "model_name": "gpt-4-turbo-2024-04-09",
}

CONFIG_AGENT_WebSearch = {
    "inc": "openai",
    "model_name": "gpt-4-turbo-2024-04-09",
}

CONFIG_AGENT_Retrieve = {
    "inc": "openai",
    "model_name": "gpt-3.5-turbo",
}

CONFIG_AGENT_yaml2json = {
    "inc": "openai",
    "model_name": "gpt-3.5-turbo-0125",
}

CONFIG_AGENT_PlanMaker_plan = {
    "inc": "openai",
    "model_name": "gpt-4-turbo-2024-04-09",
}

CONFIG_AGENT_PlanMaker_replan = {
    "inc": "openai",
    "model_name": "gpt-4-turbo-2024-04-09",
}

CONFIG_AGENT_PlanMaker_decide_update = {
    "inc": "openai",
    "model_name": "gpt-4-turbo-2024-04-09",
}

CONFIG_AGENT_ActionSeqMaker_conclude_and_decide_finish = {
    "inc": "openai",
    "model_name": "gpt-4-turbo-2024-04-09",
}

CONFIG_AGENT_CodeAnalysis = {
    "inc": "openai",
    "model_name": "gpt-3.5-turbo",
}

CODE_BASES = {
    "/home/jlhuang/CodeBases/camera": {
        "proj_path": "/home/jlhuang/CodeBases/camera",
        "language": "c++",
    },
    # "/home/jlhuang/CodeBases/jl-sdk": {
    #     "proj_path": "/home/jlhuang/CodeBases/jl-sdk",
    #     "language": "c++",
    # },
    "/home/jlhuang/CodeBases/amor": {
        "proj_path": "/home/jlhuang/CodeBases/amor",
        "language": "c++",
    },
    "/home/jlhuang/CodeBases/jasson_2.13.1_tests": {
        "proj_path": "/home/jlhuang/CodeBases/jasson_2.13.1_tests",
        "language": "c++",
    },
    "/home/jlhuang/CodeBases/md4c_0.3.3": {
        "proj_path": "/home/jlhuang/CodeBases/md4c_0.3.3",
        "language": "c++",
    },
    "/home/jlhuang/CodeBases/JL701n_v1.6.1": {
        "proj_path": "/home/jlhuang/CodeBases/JL701n_v1.6.1",
        "language": "c++",
    },
    "/home/jlhuang/CodeBases/JL701n_24.8.7": {
        "proj_path": "/home/jlhuang/CodeBases/JL701n_24.8.7",
        "language": "c++",
    },
    "/home/jlhuang/CodeBases/marshmallow-2.18.0": {
        "proj_path": "/home/jlhuang/CodeBases/marshmallow-2.18.0",
        "language": "python",
    },
    "/home/jlhuang/CodeBases/flask-a541c2ac8b05c2b23e11bd8540088fce1abc2373": {
        "proj_path": "/home/jlhuang/CodeBases/flask-a541c2ac8b05c2b23e11bd8540088fce1abc2373",
        "language": "python",
    },
    "/home/jlhuang/CodeBases/flask-06cf349bb8b69d9946c3a6a64d32eb552cc7c28b": {
        "proj_path": "/home/jlhuang/CodeBases/flask-06cf349bb8b69d9946c3a6a64d32eb552cc7c28b",
        "language": "python",
    },
}

DOC_BASES = {
    "/home/jlhuang/CodeBases/jasson_2.13.1_tests": "/home/jlhuang/DocBases/jasson_2.13.1_tests/_treedb"
}
