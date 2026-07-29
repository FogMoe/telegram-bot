# FOGMOE Account 绑定部署

Telegram Bot 通过 FOGMOE OAuth/OIDC Authorization Code + PKCE 完成账号认证。OAuth token 只在回调请求的
内存中用于验签，绑定表只保存 FOGMOE `sub`、展示用户名和审计时间。

## 前置条件

1. 在 FOGMOE Account 平台登记 `telegram_bot` 应用。
2. 为 Production 创建并启用 confidential client，scope 使用 `openid email profile`。
3. 登记一个精确 HTTPS callback，例如
   `https://bot.example.com/oauth/fogmoe/callback`。
4. 将 callback 反向代理到 Bot 的本地监听端口。

client secret 只放在服务器 `.env` 或 secret manager，不得提交到仓库、写进镜像或暴露给浏览器。
Bot 会申请邮箱和基本资料（用户名）权限，但不会将邮箱写入绑定表。

## 配置

复制 `.env.example` 中的 `FOGMOE_OAUTH_*` 配置并填写平台实际交付值。以下字段必须与 client 和 discovery
完全一致：

- `FOGMOE_OAUTH_EXPECTED_ISSUER`
- `FOGMOE_OAUTH_CLIENT_ID`
- `FOGMOE_OAUTH_CLIENT_SECRET`
- `FOGMOE_OAUTH_AUDIENCE`
- `FOGMOE_OAUTH_REDIRECT_URI`

完成配置和反向代理前保持 `FOGMOE_OAUTH_ENABLED=false`。基础 `docker-compose.yml` 不发布 OAuth
端口，因此关闭功能时不会占用宿主机端口。

启用 OAuth 时使用专用 override：

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.fogmoe-oauth.yml \
  up -d --build bot
```

override 默认仅发布 `127.0.0.1:18765`，由同机反向代理提供外部 HTTPS；该端口不应在防火墙中向公网开放。
如需换成本机其他空闲端口，请同时修改 `.env` 中的 `FOGMOE_OAUTH_LISTEN_PORT`。

## 数据库与启动

升级前先执行迁移：

```bash
.venv/bin/python -m alembic upgrade head
```

迁移会创建：

- `fogmoe_account_bindings`：可审计的绑定与软解绑历史；
- `fogmoe_binding_reward_claims`：按 Telegram ID 和 FOGMOE `sub` 双重去重的永久首次奖励记录；
- `fogmoe_oauth_transactions`：最长 10 分钟的短时 OAuth transaction。

启用后启动 Bot。回调监听失败或 OAuth 配置不完整时，进程会停止启动，不会让 `/fogmoe` 进入一个无法完成的流程。

## 验收

- 未使用 `/me` 的用户不能开始绑定；
- 群聊中的 `/fogmoe` 会提示改为私聊；
- 首次绑定增加 20 枚免费金币；
- 同一绑定重复认证不再发奖；
- `/fogmoe` 中的“解绑或更换账号”必须重新验证当前绑定的 FOGMOE `sub`；
- 软解绑后允许换绑，但 Telegram ID 或 FOGMOE `sub` 任一领过奖励都不再发奖；
- 一个 FOGMOE `sub` 不能绑定两个 Telegram 用户；
- `state` 过期、重复 callback、错误 issuer/audience/client ID/nonce/scope/signature 都会失败；
- 数据库和日志中没有 authorization code、PKCE verifier 或 token 残留。

FOGMOE 平台 endpoint、验签和 client 管理的完整契约以 AccountManagementSystem 仓库的
`docs/integration/fogmoe-oauth-client-integration-guide.md` 为准。
