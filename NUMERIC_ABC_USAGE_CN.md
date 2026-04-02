# 数值评估 ABC 运行说明

本文档用于说明如何在当前仓库中，基于已经存在的提取结果，运行数值评估 ABC，并查看输出结果。

适用场景：

- 已经完成板级提取，目录位于 `data/extraction/<board>/`
- 已经有对应的坐标与布线输入，位于 `data/datastruct/<board>/`
- 当前目标是运行数值评估，不依赖前端修改，不依赖在线模型下载

当前推荐入口脚本：

- `tools/run_numeric_real_board_eval.py`

这个脚本会基于现有提取结果构建状态，执行数值 PI 评估，并输出：

- `path_a`
- `path_b`
- `path_c`
- `knowledge_fusion`
- `numeric_eval_report.json`
- 配套的板级与 rail 级 SVG/PNG 产物

## 1. 运行前确认

在项目根目录执行：

```bash
pwd
python -V
python -m pip --version
```

确认你已经进入正确环境后，再检查输入目录是否存在：

```bash
ls data/extraction/Hi3519_PCB
ls data/datastruct/Hi3519_PCB
```

当前仓库中，`Hi3519_PCB` 对应的现有输入是：

- 提取目录：`data/extraction/Hi3519_PCB`
- 坐标文件：`data/datastruct/Hi3519_PCB/Hi3519_PCB.json`
- IPC 文件：`data/datastruct/Hi3519_PCB/Hi3519_PCB.xml`

## 2. Linux 上运行数值评估 ABC

如果你已经激活虚拟环境，直接执行：

```bash
python tools/run_numeric_real_board_eval.py \
  --board-dir data/extraction/Hi3519_PCB \
  --datastruct-json data/datastruct/Hi3519_PCB/Hi3519_PCB.json \
  --ipc-xml data/datastruct/Hi3519_PCB/Hi3519_PCB.xml \
  --output-dir data/outputs/evaluation/hi3519_numeric_abc_20260402
```

如果你希望显式使用项目虚拟环境：

```bash
source .venv/bin/activate
python tools/run_numeric_real_board_eval.py \
  --board-dir data/extraction/Hi3519_PCB \
  --datastruct-json data/datastruct/Hi3519_PCB/Hi3519_PCB.json \
  --ipc-xml data/datastruct/Hi3519_PCB/Hi3519_PCB.xml \
  --output-dir data/outputs/evaluation/hi3519_numeric_abc_20260402
```

## 3. Windows 上运行数值评估 ABC

PowerShell：

```powershell
.\.venv\Scripts\python.exe tools\run_numeric_real_board_eval.py `
  --board-dir data\extraction\Hi3519_PCB `
  --datastruct-json data\datastruct\Hi3519_PCB\Hi3519_PCB.json `
  --ipc-xml data\datastruct\Hi3519_PCB\Hi3519_PCB.xml `
  --output-dir data\outputs\evaluation\hi3519_numeric_abc_20260402
```

## 4. 重要说明

### 4.1 这是数值评估入口

这个脚本是当前仓库里最适合“基于现有提取结果跑 ABC 数值评估”的入口。

它输出的核心结果保存在：

- `numeric_eval_report.json`

其中包括：

- `pdn_evaluation.path_a`
- `pdn_evaluation.path_b`
- `pdn_evaluation.path_c`
- `pdn_evaluation.top_risks`
- `knowledge_fusion`

### 4.2 默认不要开启 datasheet bootstrap

为了避免额外命中 datasheet bootstrap / RAG / LLM 相关路径，默认不要加以下参数：

- `--enable-datasheet-bootstrap`
- `--datasheet-use-llm`

也就是说，推荐命令就是上面那条默认命令，不附加额外开关。

### 4.3 输出目录命名建议

建议使用可读命名：

`<board>_numeric_abc_<yyyymmdd>`

例如：

- `hi3519_numeric_abc_20260402`
- `t2_numeric_abc_20260402`

## 5. 运行成功后看什么

运行结束后，脚本会打印类似：

```json
{
  "output_dir": "...",
  "board": "...",
  "summary": {
    "components": ...,
    "nets": ...,
    "power_domains": ...,
    "status": "...",
    "top_risks_count": ...,
    "top_critical_rail": "..."
  }
}
```

重点检查输出目录下是否存在：

```bash
ls data/outputs/evaluation/hi3519_numeric_abc_20260402
```

应重点看到：

- `numeric_eval_report.json`
- `placement.png`
- `ipc_global.svg`
- `pdn_summary.svg`
- `curve_*.svg`
- `layout_*.svg`

## 6. 查看前端报告

运行完成后，可以直接启动报告查看器：

Linux：

```bash
python -m plagent.frontend.report_viewer data/outputs/evaluation/hi3519_numeric_abc_20260402 --host 127.0.0.1 --port 8765
```

Windows：

```powershell
.\.venv\Scripts\python.exe -m plagent.frontend.report_viewer data\outputs\evaluation\hi3519_numeric_abc_20260402 --host 127.0.0.1 --port 8765
```

浏览器打开：

- `http://127.0.0.1:8765`

如果是在 Linux 虚拟机里从宿主机访问：

```bash
python -m plagent.frontend.report_viewer data/outputs/evaluation/hi3519_numeric_abc_20260402 --host 0.0.0.0 --port 8765
```

然后在宿主机浏览器打开：

- `http://<虚拟机IP>:8765`

## 7. 常见问题

### 7.1 报 `No module named yaml`

说明当前 Python 环境缺少 `PyYAML`。

先检查：

```bash
python -m pip show pyyaml
python -c "import yaml; print(yaml.__version__)"
```

### 7.2 命令行提示 `unrecognized arguments`

通常是命令被拆坏了。Linux 下建议直接复制整行，或者确保每一行末尾的续行符 `\` 后面没有多余字符。

### 7.3 前端能打开但图不显示

优先确认你已经同步过最新的：

- `plagent/frontend/report_viewer.py`

这是 Linux 路径兼容修复的关键文件。

### 7.4 修改参数并重跑后页面不更新

同样需要使用最新的：

- `plagent/frontend/report_viewer.py`

该文件已经修复 revision/rerun 后缓存不失效的问题。

## 8. 最短命令

如果你已经装好环境，最短只要两步。

运行评估：

```bash
python tools/run_numeric_real_board_eval.py \
  --board-dir data/extraction/Hi3519_PCB \
  --datastruct-json data/datastruct/Hi3519_PCB/Hi3519_PCB.json \
  --ipc-xml data/datastruct/Hi3519_PCB/Hi3519_PCB.xml \
  --output-dir data/outputs/evaluation/hi3519_numeric_abc_20260402
```

打开前端：

```bash
python -m plagent.frontend.report_viewer data/outputs/evaluation/hi3519_numeric_abc_20260402 --host 127.0.0.1 --port 8765
```
