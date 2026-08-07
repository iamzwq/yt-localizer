# YouTube 视频字幕翻译和配音的本地 Web 工具

主要功能：

- 输入 YouTube 链接，自动下载视频和原始字幕
- 用 AI（DeepSeek）把字幕翻译成中文
- 网页上可视化调整字幕样式（字体、大小、颜色、位置等）
- 一键将字幕烧录进视频，或生成中文配音版视频

## 下载并运行项目

### 前置条件

电脑中需要安装（不会安装的话，可以让 `AI` 帮你安装）：

- Python 3
- ffmpeg（用于字幕烧录、生成配音视频）

### 克隆项目

```bash
git clone https://github.com/iamzwq/yt-localizer.git
```

### 配置翻译 / 配音密钥

翻译成中文、生成中文配音，都依赖 AI，我这里使用的是 DeepSeek API：

1. 前往 [DeepSeek 开放平台](https://platform.deepseek.com/) 注册并申请 API Key
2. 把项目根目录的 `.env.example` 复制一份，改名为 `.env`
3. 打开 `.env`，把 `DEEPSEEK_API_KEY=` 后面填上你的密钥

### 启动

终端启动

```bash
cd yt-localizer
./run.sh
```

Windows 可以双击 `run.bat` 启动

> 使用 `run.sh` 或 `run.bat` 启动后, 会自动安装依赖, 之后会在浏览器中打开本地网页, 也可以直接访问 http://localhost:8000

## 如果还是不会下载或者启动项目，让 `AI` 帮你。

**帮我下载这个项目并启动起来**

或

**帮我把这个项目启动起来**
