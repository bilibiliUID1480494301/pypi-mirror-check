# pypi-mirror-check

检查某个 PyPI 包 / 版本是否已同步到全球镜像；也能顺手列出**各镜像站当前的最新版本**。
纯标准库、零依赖，适合放进发版流水线里卡「PyPI 有了但镜像还没跟上」。

```bash
pipx run pypi-mirror-check hubrelay 0.9.5
# 或
pip install pypi-mirror-check
pypi-mirror-check hubrelay
```

## 用法

```
pypi-mirror-check <package> [version] [options]
```

| 参数 | 说明 |
|---|---|
| `version` 省略 | 以**官方最新版**作为目标版本 |
| `--wait --interval 30 --timeout 900` | 发版后轮询，直到全部镜像同步或超时 |
| `--json` | 机读输出（含每个镜像的 `latest` / `version_count`） |
| `--all-versions` | 列出各镜像上的**全部版本**（查各站版本号） |
| `--only tuna,aliyun` | 只查指定镜像（key 见 `--list-mirrors`） |
| `--require-hash` | 镜像页没给 `sha256` 也算未同步 |
| `--config FILE` | 自定义镜像清单（默认读包内 `mirrors.json`；也可用环境变量 `PYPI_MIRROR_CHECK_CONFIG`） |
| `--mirror KEY=LABEL=URL` | 临时追加/覆盖一个镜像（可重复，URL 需含 `{name}`） |
| `--official URL` | 覆盖官方 JSON 基址 |
| `--list-mirrors` | 打印当前配置来源与镜像表 |

## 输出

```
包 hubrelay · 目标 0.9.5 · 期望 2 个文件
镜像            站点最新  状态    延迟     备注
PyPI 官方       0.9.5     已同步  312ms
清华 TUNA       0.9.5     已同步  180ms
阿里云          0.9.3     缺失    -       缺 hubrelay-0.9.5.tar.gz, hubrelay-0.9.5-py3-none-any.whl
```

## 判定逻辑

不看「有没有这个版本号」就完事，而是比对**文件名 + `sha256`**：

- 目标版本的每个文件都必须在该镜像的 simple 页出现；
- 页面里带 `#sha256=` 时逐个比对（截断/换源会被抓出来）；
- 镜像不给 hash 时退化为「文件名存在」，除非 `--require-hash`。

`站点最新` 是从 simple 页的文件名里解析出的最大版本（支持 `a/b/rc/post/dev` 排序）。

## 镜像配置（不写死在代码里）

默认读包内 `mirrors.json`，格式：

```json
{
  "official_json": "https://pypi.org/pypi",
  "mirrors": [
    { "key": "pypi", "label": "PyPI 官方", "simple": "https://pypi.org/simple/{name}/" },
    { "key": "tuna", "label": "清华 TUNA", "simple": "https://pypi.tuna.tsinghua.edu.cn/simple/{name}/" }
  ]
}
```

想加内网/自建源，复制一份改 JSON，用 `--config` 指过去即可，无需改代码。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 所有镜像都已同步 |
| 1 | 有镜像缺失 / 不完整 / sha256 不一致 |
| 2 | 参数错误，或官方源不可用 |
| 3 | `--wait` 超时仍有镜像未同步 |

## CI 用法

```yaml
- run: pipx run pypi-mirror-check "${{ env.PKG }}" "${{ env.VER }}" --wait --timeout 600
```

---

## English

`pypi-mirror-check` checks whether a given PyPI package/version has propagated to
global mirrors, and shows each mirror's current latest version. Stdlib-only.

It compares **filenames + sha256** (not just "version exists"), supports
`--wait` polling for post-release sync, `--json`, `--all-versions`, and a
**config-file mirror list** (`--config` / `PYPI_MIRROR_CHECK_CONFIG`) so mirrors
are data, not code.

Exit codes: `0` all synced · `1` missing/mismatched · `2` bad args or official
unreachable · `3` `--wait` timeout.

## License

MIT
