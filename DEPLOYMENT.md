# CY16 安全部署手册

这个看板是独立的辅助服务。它有自己的代码、镜像、容器、SQLite 数据和回滚记录，不进入 `new-api` 的发布链。现有公网链路保持不变：

```text
https://ai.sandboxai.top/upstream-balance/
  -> CY16 Nginx
  -> light-proxy:4000
  -> 127.0.0.1:8756
  -> upstream-balance 容器
```

发布脚本只允许替换 `upstream-balance` 容器；不会重启或修改 Nginx、light-proxy、new-api，也不会执行 `docker compose down`、`rsync --delete` 或全局镜像清理。

## 资源结论

2026-07-25 上线前快照：CY16 为 8 核、7.8 GiB 内存，当时约有 5.8 GiB 可用，磁盘约有 14 GiB 可用。看板集成 17 个 OpenCode 账号的实测峰值约 63 MiB；生产容器仍设置 512 MiB 硬上限、192 MiB 预留、768 MiB 内存加交换区上限、1 CPU 和 128 PID。因此当前资源足够，且单个看板异常不能无界挤占主业务资源。

## 权限边界

- 普通开发员工：只能创建功能分支、提交 PR、查看 CI；不能持有 CY16 SSH 私钥，不能加入 CY16 的 Docker 组，不能直接推送 `main`。
- 审核人：至少一人批准 PR，且 `verify` 必须通过。
- 受信任发布人：合并后在已配置 `gh` 和 `cy16` SSH alias 的发布工作站运行专用脚本。
- 生产凭据：只存在 CY16 的 `/home/ubuntu/upstream-balance/.env`、`data/`，或首次迁移时的本地 `0600` 文件中；绝不进入 Git、Docker 构建上下文、CI artifact 或命令输出。

仓库当前是公开仓库。任何 PR 都必须确认没有账号 Cookie、API Key、数据库、`.env`、部署日志或真实配置文件；这些路径已经同时被 `.gitignore` 与 `.dockerignore` 排除，但提交前仍要检查。

## CI 门禁

每个 PR 和 `main` 推送都会执行：

1. 用固定 commit 的 Gitleaks 扫描完整 Git 历史，阻断凭据误提交。
2. 使用 hash 锁定的 Python 依赖和 npm lockfile 安装依赖。
3. `npm audit --audit-level=high`。
4. 构建前端并确认生成后的 `static/` 已提交且无差异。
5. 在隔离数据目录运行全部 Python 测试和部署契约测试。
6. 校验生产 Compose。
7. 用 digest 固定的 Python 基础镜像构建容器。
8. 以只读根文件系统、无 Linux capabilities、有限内存和 PID 的方式做鉴权与数据库 smoke test。
9. 仅在 `main` 推送时，把刚才实际测试过的同一镜像保存为短期 CI artifact，并上传为 `ghcr.io/chenzr888/light-metapi:sha-<40位commit>`；发布任务不会重新构建另一个镜像。

GitHub 的 `main` 必须开启保护：禁止 force push/删除，必须通过 `verify`，必须保持分支最新，并至少获得一人审批。CI 文件本身不能替代这项仓库设置。

## 日常开发流程

```bash
git switch main
git pull --ff-only
git switch -c feature/<简短名称>

# 开发完成后
scripts/verify-release.sh
git status --short
git push -u origin feature/<简短名称>
gh pr create --base main
```

由另一名审核人批准，CI 通过后再合并。不要从 dirty worktree 发布，也不要把功能分支直接部署到 CY16。

更新 Python 依赖时，先修改 `requirements.in`，再生成带 hash 的锁文件：

```bash
uv pip compile requirements.in --generate-hashes --output-file requirements.txt
```

## 正式发布

先同步并查看只读发布计划：

```bash
git switch main
git pull --ff-only
release_sha=$(git rev-parse HEAD)
scripts/deploy-cy16.sh --sha "$release_sha" --plan
```

计划会拒绝以下情况：worktree 不干净、当前不是 `main`、SHA 不是 `origin/main`、该 SHA 的 GitHub CI 未成功、镜像标签不是精确 SHA，或首次导入文件权限不是 `0600`。

普通版本升级不会再次导入账号：

```bash
scripts/deploy-cy16.sh \
  --sha "$release_sha" \
  --confirm "cy16:${release_sha:0:12}"
```

仅第一次迁移 OpenCode 账号时增加一次性参数：

```bash
scripts/deploy-cy16.sh \
  --sha "$release_sha" \
  --import-opencode /absolute/path/to/config.json \
  --confirm "cy16:${release_sha:0:12}"
```

导入文件必须是 `0600`。脚本先核对账号数，上传到仅当前用户可读的 release 目录；候选和正式容器成功导入并加密后会删除明文，退出 trap 也会清理远端暂存文件。

## CY16 切换顺序

专用远端脚本由 `nohup + setsid` 脱离 SSH 会话运行，并用 PID、日志和原子状态文件供本地轮询；断网或关闭终端不会终止远端发布。脚本固定主机名、生产目录、容器名、监听地址和公网地址，然后顺序执行：

1. 每次发布先用 128-bit 随机 nonce 原子创建 `releases/<SHA>/<attempt-id>` 独立暂存目录，再由 `flock` 获取部署锁；两个员工同时操作时不会覆盖脚本、镜像或导入文件，未获得锁的一方在切换前退出。
2. 通过 GitHub API 获取该 SHA 成功 CI run 的唯一 artifact ID，以 16 路分段下载所保存的已测试镜像；下载后核对 API 声明大小，传到 CY16 后复核 SHA256、载入镜像并核对 OCI revision label。临时签名 URL 不写日志，CY16 不需要保存 GitHub 凭据。
3. 给旧镜像增加不可变回滚 tag。
4. 用 SQLite online backup API 备份数据库，合并 WAL、切换为单文件 DELETE journal，并对独立快照执行 `integrity_check`；同时备份加密密钥和 Session 密钥。
5. 在生产备份副本上启动 `127.0.0.1:18756` 候选容器。候选关闭定时刷新和通知，验证数据库迁移、用户数、渠道数、OpenCode 账号数、鉴权和容器硬化参数。
6. 候选通过后才把一次性导入文件放入生产数据目录，并只替换看板容器。
7. 验证容器健康、SQLite `quick_check`、管理员仍存在、未登录访问受保护接口返回 401、账号数、静态资源、公网完整链路、重启次数和数据权限。
8. 任一步失败：切换前保持生产不动；切换后使用备份中固化的独立 rollback Compose 自动恢复旧镜像、数据库、两个密钥和切换前的 Compose 状态；回滚不依赖本次候选 Compose。

每次成功输出 `backup=<备份ID>`，并写入 CY16 的 `/home/ubuntu/upstream-balance/deployments.log`。发布工作站随后通过 SSH 下载一份 SHA256 校验过的完整备份到 `$HOME/ai/api/.backups/upstream-balance/`（目录 `0700`、文件 `0600`），因此单台 CY16 磁盘故障时仍有站外副本。

## 手工回滚

若部署当时成功、后来才发现业务问题，由受信任发布人使用当次输出的备份 ID：

```bash
scripts/rollback-cy16.sh \
  --backup 20260725T120000Z-<40位发布SHA> \
  --confirm cy16:rollback:20260725T120000Z
```

手工回滚也使用同一把部署锁，并直接运行目标备份中经 manifest 校验的回滚脚本、镜像归档和 rollback Compose。它会先为当前状态再做一份 emergency backup 并保留当前镜像；若旧版本启动或公网验证失败，会自动恢复回滚前状态。普通员工不要在生产机上手工执行 `docker rm`、复制 SQLite 文件或修改代理配置。

## 验收标准

- 公网首页 `https://ai.sandboxai.top/upstream-balance/` 返回 200，静态资源可加载。
- `/_ub_api/auth/bootstrap` 返回 `needs_setup=false`，原看板管理员登录凭据继续使用。
- 未登录请求 `/_ub_api/opencode/accounts` 返回 401。
- OpenCode 子页显示预期账号数；首次上线为 17，后续发布保持上线前账号数。
- `users`、`channels` 数量不变，SQLite 检查为 `ok`。
- 容器为 `healthy`、`RestartCount=0`，只绑定 `127.0.0.1:8756`，资源限制生效。
- Nginx、light-proxy、new-api 的容器 ID和启动时间不变。
