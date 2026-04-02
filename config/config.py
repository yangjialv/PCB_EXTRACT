import os, sys
import importlib.util
from dpagent.config.defconfig import *


def read_local_config():
    if "DPAGENT_LOCALCONFIG_FILE" in os.environ:
        local_config_file = os.environ.get("DPAGENT_LOCALCONFIG_FILE")
    else:
        local_config_file = os.path.join(os.getcwd(), ".config.py")
    if os.path.exists(local_config_file):
        with open(local_config_file, "r") as file:
            code = file.read()
        exec(code, globals())


class ApiConfig:
    def __init__(self):
        # openai
        self.OPENAI_API_BASE = OPENAI_API_BASE
        self.OPENAI_API_KEY = OPENAI_API_KEY
        os.environ["OPENAI_API_BASE"] = OPENAI_API_BASE
        os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY

        # langsmith
        self.USE_LANGSMITH = USE_LANGSMITH
        if self.USE_LANGSMITH:
            os.environ["LANGCHAIN_TRACING_V2"] = "true"
            self.LANGCHAIN_API_KEY = LANGCHAIN_API_KEY
            os.environ["LANGCHAIN_API_KEY"] = LANGCHAIN_API_KEY
            self.LANGCHAIN_PROJECT = LANGCHAIN_PROJECT
            os.environ["LANGCHAIN_PROJECT"] = LANGCHAIN_PROJECT

        # tavily
        if TAVILY_API_KEY:
            os.environ["TAVILY_API_KEY"] = TAVILY_API_KEY


class AgentAssignModelConfig:
    def __init__(self):
        self.default = CONFIG_AGENT_Default
        self.ExceedLength = CONFIG_AGENT_ExceedLength
        self.CodeAnalysis = CONFIG_AGENT_CodeAnalysis
        self.retry = CONFIG_AGENT_Retry
        self.WebSearch = CONFIG_AGENT_WebSearch
        self.Retrieve = CONFIG_AGENT_Retrieve
        self.yaml2json = CONFIG_AGENT_yaml2json
        self.PlanMaker_plan = CONFIG_AGENT_PlanMaker_plan
        self.PlanMaker_replan = CONFIG_AGENT_PlanMaker_replan
        self.PlanMaker_decide_update = CONFIG_AGENT_PlanMaker_decide_update
        self.ActionSeqMaker_conclude_and_decide_finish = CONFIG_AGENT_ActionSeqMaker_conclude_and_decide_finish


read_local_config()

# categorized config
apiConfig = ApiConfig()
agentMdlCfg = AgentAssignModelConfig()
