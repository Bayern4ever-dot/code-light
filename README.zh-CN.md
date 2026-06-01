# ⚡ code-light

[English](README.md) | 中文

轻量级桌面状态监控工具，专为 AI 编程工作流设计。

code-light 通过 VS Code 扩展监控 Codex 和 Claude Code 的状态，在系统托盘和桌面悬浮窗中实时显示，追踪 token 用量和配额，并支持一键跳转到 VS Code 窗口。

## 功能特性

- 🖥️ **系统托盘图标** - 带颜色编码的动态状态指示器
- 🪟 **桌面悬浮窗** - 始终置顶的桌面小组件，实时显示状态
- 📊 **Web 仪表盘** - 详细的用量统计和历史记录
- 🤖 **Claude Code 监控** - 追踪 DeepSeek/Mimo API token 消耗
- 💻 **Codex 监控** - 追踪 OpenAI 配额使用情况
- 🚀 **一键跳转** - 立即聚焦到 VS Code 窗口
- 🔔 **配额预警** - 在触及限制前收到提醒

## 截图

### 系统托盘

<img src="docs/status.png" width="300" alt="系统托盘状态指示器">

### 桌面悬浮窗

<img src="docs/widget.png" width="450" alt="桌面悬浮小组件">

### Web 仪表盘

<img src="docs/dashboard.png" width="100%" alt="Web 仪表盘">

## 安装

### 前置要求

- Windows 10/11
- Python 3.11+
- [uv](https://github.com/astral-sh/uv) 包管理器

### 使用 uv 安装

```bash
# 克隆仓库
git clone https://github.com/Bayern4ever-dot/code-light.git
cd code-light

# 安装依赖
uv sync

# 运行
uv run code-light
```

### 作为包安装

```bash
# 从源码安装
uv pip install -e .

# 运行
code-light
```

## 使用方法

### 基本用法

```bash
# 使用默认设置启动
code-light

# 启用调试日志
code-light --debug

# 启动时隐藏悬浮窗
code-light --no-floating

# 自定义仪表盘端口
code-light --port 8080

# 自定义轮询间隔（秒）
code-light --poll-interval 60

# 自定义悬浮窗透明度
code-light --opacity 0.9
```

### 系统托盘

- **左键点击**：打开仪表盘
- **右键点击**：上下文菜单
- **状态颜色**：
  - 🟢 绿色：工作中
  - 🟡 黄色：等待输入
  - 🟠 橙色：配额预警
  - 🔴 红色：错误
  - ⚫ 灰色：空闲/离线

### 悬浮窗

- **拖拽**：移动窗口
- **双击标题栏**：打开仪表盘
- **点击代理卡片**：聚焦 VS Code 窗口
- **✕ 按钮**：隐藏窗口

### 仪表盘

访问 `http://127.0.0.1:7681`（默认端口）。

功能：
- 实时状态监控
- Token 用量统计（7 天、30 天）
- 任务历史记录
- 成本追踪
- 一键聚焦 VS Code

## 架构

```
code-light/
├── src/code_light/
│   ├── __main__.py          # 入口
│   ├── app.py               # 应用编排器
│   ├── config.py            # 配置（不可变 dataclass）
│   ├── state.py             # SQLite 状态持久化
│   ├── models.py            # 数据模型
│   ├── monitors/
│   │   ├── claude_code.py   # Claude Code 会话监控
│   │   ├── codex.py         # Codex 配额监控
│   │   └── process.py       # VS Code 进程检测
│   ├── services/
│   │   ├── quota.py         # 配额追踪与预警
│   │   ├── token_counter.py # Token 计数与成本
│   │   └── vscode.py        # VS Code 窗口管理
│   └── ui/
│       ├── tray.py          # 系统托盘 (pystray)
│       ├── floating.py      # 悬浮窗 (tkinter)
│       └── dashboard.py     # Web 仪表盘 (Flask)
└── dashboard/
    ├── templates/           # Jinja2 模板
    └── static/              # CSS、JS、图标
```

## 数据来源

### Claude Code

- 会话文件：`~/.claude/projects/**/*.jsonl`
- 解析 JSONL 获取 token 用量、模型、时间戳
- 追踪 DeepSeek/Mimo API token 消耗

### Codex

- 认证文件：`~/.codex/auth.json`
- 用量 API：`chatgpt.com/backend-api/wham/usage`
- 速率限制追踪（主窗口/次窗口）
- 积分余额监控

### VS Code

- 通过 Win32 API 检测窗口
- 使用 psutil 枚举进程
- 解析窗口标题获取项目/代理信息

## 开发

### 环境搭建

```bash
# 克隆
git clone https://github.com/Bayern4ever-dot/code-light.git
cd code-light

# 安装开发依赖
uv sync --dev

# 运行测试
uv run pytest

# 运行代码检查
uv run ruff check .

# 运行类型检查
uv run mypy src/
```

### 项目结构

- `src/code_light/`：主包
- `dashboard/`：Web 仪表盘前端
- `tests/`：测试套件

## 许可证

MIT License

## 致谢

- [claudebar](https://github.com/mryll/claudebar) - Claude 订阅监控（Waybar 组件）
- [codexbar](https://github.com/mryll/codexbar) - Codex 订阅监控（Waybar 组件）
- [TokenTracker](https://github.com/mm7894215/TokenTracker) - 多工具 token 追踪
