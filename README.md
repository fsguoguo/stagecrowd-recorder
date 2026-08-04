# stagecrowd-recorder

容器内归档 Widevine 加密的 HLS 直播流。取密钥、下载、解密、封装。

## 功能

| 能力 | 说明 |
|---|---|
| 密钥获取 | 用本地 CDM 重放 license 请求，取出 content key |
| 覆盖校验 | 校验密钥是否覆盖**实际会被录制的轨道**，不是 manifest 声明的全部 |
| 轮换监视 | 录制期间定期复查，广播中途换密钥会告警 |
| 下载解密 | 交给 N_m3u8DL-RE + shaka-packager |
| 边录边播 | 混流文件按播放速率写入，录制中即可播放 |
| 分片保留 | 默认保留已解密分片，中断后可重建 |
| 重建 | 从分片重建可播放文件，逐轨报告缺失 |

## 部署

需要 Docker 和 Docker Compose。

### 1. 准备配置文件

项目根目录放一个  `.env`（compose 挂载的是 `.env`，容器内会成为 `/config/.stagecrowd`）：

```ini
m3u8=https://fastly.live.brightcove.com/.../playlist-hls.m3u8
wv_token=eyJhbGciOiJIUzI1NiIs...
```

`m3u8` 是流地址，建议指向 master 播放列表。`wv_token` 是从播放页面取到的 license session token。

### 2. 准备 CDM

把 Widevine 设备文件放在项目根目录，命名 `device.wvd`。项目不附带此文件。

没有 CDM 时可以跳过这步，改用 `--key` 直接提供密钥。此时需要注释掉 `compose.yaml` 里这一行，
否则 Docker 会在源位置创建目录：

```yaml
# - ./device.wvd:/config/device.wvd:ro
```

### 3. 构建

```powershell
docker compose build
```

可选的自检：

```powershell
docker compose run --rm recorder probe
```

### 4. 开始录制

```powershell
docker compose up
```

按 `Ctrl+C` 停止。

需要在直播**开始之前**运行。HLS 是滑动窗口 manifest，不保留历史分片，错过的开头无法追回。

## 命令

| 命令 | 作用 |
|---|---|
| `capture` | 完整流程：解析流 → 取密钥 → 覆盖校验 → 录制 |
| `plan` | 同上但只打印将执行的命令，不实际运行 |
| `keys` | 只取密钥并打印覆盖情况 |
| `rebuild` | 从保留的分片重建可播放文件 |
| `probe` | 环境自检，不需要流地址和 token |

## 参数

`capture` / `plan` / `keys` 共用：

| 参数 | 说明 |
|---|---|
| `--url URL` | m3u8 地址 |
| `--out DIR` | 输出目录，默认 `run_<时间戳>.out` |
| `--token TOK` | license session token |
| `--license-url URL` | 完整 license 地址，优先于 `--token` |
| `--headers-file FILE` | `--license-url` 用的请求头，每行 `Name: value` |
| `--key KID:KEY` | 直接提供密钥，可重复或逗号分隔。跳过 license 步骤 |
| `--cdm PATH` | 设备文件路径，默认 `/config/device.wvd` |
| `--decryptor {SHAKA_PACKAGER,MP4DECRYPT}` | 解密引擎，默认 `SHAKA_PACKAGER` |
| `--allow-partial-keys` | 允许部分轨道无密钥，那些轨道不会被解密 |
| `--discard-shards` | 不保留分片，省一半磁盘，代价是失去 `rebuild` |
| `--burst-output` | 尽快落盘，不按播放速率。录制中的文件将无法播放 |
| `--quiet-shards` | 不在控制台打印逐分片进度 |
| `--no-shard-log` | 完全不跟踪分片 |
| `--verbose-downloader` | 显示下载器自身的日志 |
| `--guard-interval S` | 密钥轮换复查间隔，默认 240 秒 |
| `--settings FILE` | 配置文件路径。未指定时：设了 `$STC_SETTINGS` 就只用它，否则找 `./.stagecrowd` 或 `./.env` |

`rebuild` / `probe` 不接受 `--settings`，要指定配置文件请写在子命令**之前**
（`stagecrowd-recorder --settings FILE probe`）或用 `$STC_SETTINGS`。配置文件对每个命令
都会加载，只是 `rebuild` 不从中取任何值，`probe` 取——它的 `--cdm` 默认值来自配置文件写入的环境变量。

退出码：`0` 成功，`1` 环境/工具链检查未通过，`2` 运行时错误，`130` 用户中断。
参数写错（含对 `rebuild` / `probe` 用 `--settings`）由 argparse 报告，同样是 `2`。

## 常见用法

```powershell
# 默认录制
docker compose up

# 已有密钥的离线录制，不需要 CDM
docker compose run --rm recorder capture --url "<m3u8>" --key kid1:key1 --key kid2:key2

# 只取密钥并检查覆盖，不录制
docker compose run --rm recorder keys --url "<m3u8>" --token "<token>"

# 从中断的录制重建
docker compose run --rm recorder rebuild archive/run_20260803_091917.out
```

## 配置

优先级：命令行参数 > 环境变量 > 配置文件 > 默认值。

配置文件只认 `KEY=value`，支持短别名：

| 别名 | 规范变量 |
|---|---|
| `m3u8` / `url` / `stream` | `STC_URL` |
| `wv_token` / `token` | `STC_TOKEN` |
| `license` / `license_url` | `STC_LICENSE_URL` |
| `key` / `keys` | `STC_KEYS` |
| `cdm` / `device_wvd` | `STC_CDM` |
| `out` / `output` | `STC_OUT` |
| `headers` | `STC_HEADERS` |

其余变量：`STC_SETTINGS`、`STC_DOWNLOADER`、`STC_FFMPEG`、`STC_SHAKA`、
`STC_MP4DECRYPT`、`HTTPS_PROXY`、`NO_COLOR`。
