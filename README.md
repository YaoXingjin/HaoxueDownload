# Haoxue Download
基于好学App的课程回放下载路线

## 环境要求

- Python 3.10 或更高版本
- `ffmpeg`，用于解析 m3u8 并转换为 mp4；需要安装到系统并加入 `PATH`

## 使用 uv（推荐）

项目使用 `pyproject.toml` 声明依赖，并使用 `uv.lock` 锁定版本。安装 [uv](https://docs.astral.sh/uv/) 后，在项目目录执行：

```powershell
# 创建或同步项目虚拟环境并安装锁定依赖
uv sync

# 在项目环境中运行
uv run python haoxue_download.py
```

更新依赖时修改 `pyproject.toml`，然后执行 `uv lock` 更新锁文件；提交代码时一并提交 `pyproject.toml` 和 `uv.lock`。

## 使用裸 Python

也可以不使用 uv，直接用任意 Python 3.10+ 环境安装依赖：

```powershell
python -m pip install requests pycryptodome
python haoxue_download.py
```

然后填写学号、密码，选择课程、节次并下载回放。

如果已经通过 uv 创建了环境，也可以直接调用其中的 Python：

```powershell
.venv\Scripts\python.exe haoxue_download.py
```

## 运行

程序启动后会依次要求输入账号、密码、课程搜索条件和课程/节次编号。

调试 HTTP、API 和 ffmpeg 信息时可以添加 `--verbose`：

```
python haoxue_download.py --verbose
```
