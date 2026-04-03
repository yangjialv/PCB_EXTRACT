# RoutagentPCB

**RoutagentPCB** 是一个基于大模型与知识图谱的 PCB 信号完整性（SI）智能分析系统。系统构建了符合工程师设计思维的层次化知识图谱，并提供电气分析计算工具，能够对信号线进行单端阻抗、差分阻抗和时延维度的电气性能分析，给出对线宽、线间距和线长差的量化评估结果。



[TOC]



------

## ✨ 功能特性

1. 层次化知识图谱构建
   - 基于 SI 知识文档，自动构建符合工程师设计思维的知识图谱。
   - 支持实体关系抽取与存储至 Neo4j。
2. 电气性能智能分析 (SI Analysis)
   - 结合层叠信息，计算单端/差分阻抗。
   - 分析信号时延维度性能。
   - 自动调用后端 Python 脚本执行分析任务。量化评估线宽、线间距、线长差。

------

## 🛠 系统架构与环境

本系统依赖以下运行环境，请确保在执行前已完成配置。

| 组件         | 版本/规格                 | 说明               |
| :----------- | :------------------------ | :----------------- |
| **环境**     | `Conda: Qwen72B`          | 大模型推理环境     |
| **数据库**   | `Neo4j 5.26.24`           | 知识图谱存储与查询 |
| **语言**     | `Python 3.10`             | 后端脚本执行       |
| **输入格式** | `IPC2581 (.xml)`, `.dcfx` | PCB 设计文件       |

------

## 🚀 快速开始

### 1. 激活运行环境

```bash
conda activate Qwen72B
```

### 2. 启动知识图谱数据库

确保 Neo4j 服务正在运行，否则 SI 分析将无法获取知识库支持。

```bash
cd .../RoutagentPCB/neo4j-community-5.26.24
bin/neo4j console
```

*默认访问地址：`http://10.175.57.236:8181`*

### 3. 验证连接

使用以下 Cypher 语句验证图谱是否可视：

```cypher
MATCH (n)-[r]-(m) RETURN n, r, m
```

------

## 🎯 技能使用说明

本系统提供两个核心技能（Skill），大模型可根据用户意图自动调用。

### 技能 1：构建知识图谱 (`build_knowledge_graph`)

- **适用场景**：系统初始化、更新设计规则库、导入新文档。

- 输入要求

  - 知识文档目录：.../RoutagentPCB/dpagent/Experience/input/.md
  - 工作流配置：.../RoutagentPCB/dpagent/Experience/GraphRag/workflow_new.yaml

- 执行命令

  ```bash
  python .../RoutagentPCB/dpagent/Experience/GraphRag/main.py --config .../RoutagentPCB/dpagent/Experience/GraphRag/workflow_new.yml
  ```

### 技能 2：SI 信号完整性分析 (`analyze_si`)

- **适用场景**：用户询问阻抗、时延、线宽评估等电气性能问题。

- **前置条件**：**必须确保知识库（Neo4j）处于运行状态。**

- 输入要求

  - `PCB_ipc2581 可制造文件.xml`
  - `PCB_XMLconstraints_file.dcfx` (推荐)

- 执行命令

  ```bash
  python .../RoutagentPCB/dpagent/Interaction/quick_test.py
  ```



**补充：**

利用PCB_XMLconstraints_file.dcfx 获取线网的阻抗匹配值差分信号列表，运行 ：

python ....RougentPCB\dpagent\Interaction\agents\testlineconstruct\agent.py   

可以设置采样数量，默认差分组x2 ,单端 x2

在同一文件夹下会生成  PCB_requirement.json 文件（quick_test）运行需要，放入input文件夹CS

并指定quick_test.py 的parser参数

--requirements     path:PCB_requirement.json      --xml     path:PCBipc2581.xml

------

## ⚙️ 配置文件与逻辑

系统配置位于 `.../RoutagentPCB/Interaction/config`。

### 核心配置表

| 文件名            | 用途              | 必填性                |
| :---------------- | :---------------- | :-------------------- |
| `mapping.json`    | 公式索引表        | **必填**              |
| `net_filter.json` | 处理对象筛选表    | 条件必填 (见下方逻辑) |
| `net_rules.json`  | 线网 - 阻抗匹配表 | 条件必填 (见下方逻辑) |

### ⚠️ 重要逻辑规则

1. 约束文件优先级
   - 如果提供了 `PCB_XMLconstraints_file.dcfx` 文件，系统将**自动忽略** `net_filter.json` 和 `net_rules.json`。
   - 如果**未提供** `.dcfx` 文件，则必须配置 `net_filter.json` 和 `net_rules.json` 才能进行分析。
2. 知识库依赖
   - 执行 SI 分析前，必须确认知识图谱已构建且 Neo4j 服务正常。

------

## 📂 目录结构

```text
RoutagentPCB/
├── neo4j-community-5.26.24/   # 数据库
└──dpagent/
    ├── Experience/
    │   ├── input/              # 知识文档输入
    │   └── GraphRag/
    │       ├── main.py            # 图谱构建入口
    │       ├── workflow_new.yaml  # 图谱构建配置
    │       └── document/
    │           ├── agents/        # 知识库 Agent 实例
    │           └── index/         # 知识库 Agent 辅助功能
    ├── Interaction/
    │   ├── config/            # 系统配置 (mapping, rules, filter)
    │   ├── quick_test.py      # SI 分析入口
    |	├── input/             # xml,dcfx文件入口
    │   ├── output/            # 分析报告输出
    │   ├── agents/            # 布线解析 Agent 实例
    │   └── index/			   # 布线解析 Agent 辅助功能
    ├── dpagent/
    │   └── Constrain_generate/
    │      └── scripts/       # 公式脚本位置
    └── README.md
```

------

## 📤 输出与结果

### 1. 知识图谱结果

- **存储位置**：Neo4j 数据库
- **访问地址**：`http://10.175.57.236:8181`
- **查询示例**：`MATCH (n)-[r]-(m) RETURN n, r, m`

### 2. SI 分析报告

- **文件格式**：文本报告 (`.txt`)
- **存储位置**：`.../RoutagentPCB/dpagent/Interaction/output/experiment_report.txt`
- 内容包含线宽、线间距、线长差的量化评估
  - 单端/差分阻抗计算值
  - 时延分析结果





