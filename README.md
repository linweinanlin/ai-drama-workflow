# AI 漫剧自动化工作流

> 把一部竖屏漫剧从「剧本」推到「可交付成片」的本地桌面工作台。
> **本仓库是源码仓，私有。** 对外展示页见 `ai-drama-workflow-showcase`。

Python 3.9 · PySide6（外壳）· Tk/ttk（生产内核）· FFmpeg · OpenCV · MediaPipe · 单机本地运行

---

## 它解决什么问题

做一个镜头容易，做完一集难。真正卡人的是一致性、可追溯、可返工：

- 角色脸不能漂、场景不能串；
- 台词要贴上口型，配音比画面长时不能硬切；
- 每镜时长要卡在模型能接受的区间；
- 某一镜废了，要能只重跑那一镜，而不是整集重来；
- 交付时要有登记，出问题能顺着任务号查回去。

这个工作台把 **10 个生产阶段 + 8 个功能页签** 固化进桌面应用，每步都有明确的输入、产物与通过条件。

## 架构：外壳与内核分离

```
AI 漫剧自动化工作流
├── app.py              PySide6 单窗口外壳：品牌栏 + 一个页面
├── legacy_adapter.py   适配器：进程内改品牌常量，不改内核一行逻辑
├── legacy_core/        生产内核（Tk/ttk，18 个模块）
├── assets/             图标
├── styles.qss          外壳样式
└── launch.py           启动入口（异常落盘到 startup.log）
```

- 内核跑在**独立进程**里，独占 Tk 主循环；外壳用 `QWindow.fromWinId` +
  `createWindowContainer` 把它作为原生子窗口挂进 Qt 页面，两侧不共享事件循环。
- 这样切页不丢状态、内核崩了不拖垮外壳，内核也不需要知道自己被谁包着。
- 外壳与内核之间只走一条极窄的管道：`@@LEGACY {...}` 结构化事件（ready / state / description）
  与 `{"command":"close"}` 控制指令。

## 8 个功能页签

| 页签 | 做什么 |
|---|---|
| 项目总览 | 项目元信息、当前阶段、任务/成功率/交付版本统计 |
| 生产流程 | 01–10 阶段看板，完成度按项目目录里**真实存在的产物**反推 |
| 剧本分析 | 剧本拆解为镜头与台词，按内容指纹缓存结果 |
| 分镜墙 | 镜头卡片网格：缩略图、运镜、台词、配音状态、风险标记，可拖拽排序 |
| 生成执行 | 逐镜提交、并发 1–4、费用预估、任务号即时入台账 |
| 质检交付 | 逐镜核对视频/时长/配音/台词/帧链/场景，人工标记通过或返工 |
| 任务台账 | 每镜的任务号、阶段、模型、状态、衔接帧、审核结论 |
| 运行日志 | 子进程真实 stdout/stderr，不做「美化后」的假进度 |

## 关键实现

- **状态机 + 六项质量闸门**：阶段推进按产物判定，缺产物就是 0%；质检闸门覆盖
  视频存在、时长合规、配音存在、台词匹配、帧链连续、场景一致。
- **异步提交与断点续跑**：提交即把 `task_id` 写进本地台账，界面崩了、断网了，重开还能接着查。
- **单镜时长 4–8 秒**：模型对 9 秒会直接拒绝，`min/max_shot_duration_seconds` 收敛在配置层。
- **配音为时间基准**：MediaPipe FaceMesh 逐帧测嘴部开合（MAR）定位嘴动窗口，
  超长台词用 `atempo` 压缩而不是拉伸画面；字幕按停顿 > 0.5s 切分，输出 SRT/ASS 并烧录。
- **Token 控制**：剧本分析按内容指纹缓存、单镜上下文只带本镜行与引用路径、提示词去重限长。
- **交付登记**：逐镜结论写入 `release-manifest.csv`。

## 运行

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python launch.py
```

或双击 `启动漫剧工作流.cmd`（自动建虚拟环境、补依赖）。

首次启动会读取 `legacy_app_config.json`（不入库）指定项目根目录：

```json
{
  "workflow_root": "D:/ai-short-drama-workflow",
  "active_project": "",
  "dashboard_path": "",
  "version": "2.1.0"
}
```

参考 `legacy_app_config.example.json`。项目目录约定（`01_bible/`、`02_script/`、
`03_storyboard/`、`05_generation/`、`07_qc/`、`08_release/`）由内核按产物反推阶段进度。

## 运行环境

- Windows 10/11，Python 3.9
- FFmpeg / ffprobe 在 PATH 中
- 模型服务由使用者自行配置（阿里云百炼 wan 视频、CosyVoice TTS 等），密钥只存本机
- 不内置模型权重、不代付调用费用、不伪造生成结果

## 许可

源码可见（source-available），**非** OSI 开源：可读、可学、可自用；
复制、改名分发、商用与再分发需取得作者书面许可。详见 `LICENSE` 与 `NOTICE`。
