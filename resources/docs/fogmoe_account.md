# FOGMOE Account

FOGMOE Account 是 FOGMOE 服务共用的账号身份。用户可以在
`https://account.fog.moe/` 注册、登录和管理账号，也可以在授权应用页面查看或撤销曾经允许的应用。

## 在 Telegram Bot 中绑定

- 私聊机器人发送 `/fogmoe`。
- 尚未注册机器人资料时，先发送 `/me`。
- 点击机器人给出的按钮，在 FOGMOE Account 页面登录并授权邮箱和基本资料（用户名）。
- 成功后，FOGMOE Account 与 Telegram 账号会建立一对一绑定。
- 首次成功绑定奖励 20 金币；同一用户重复认证不会再次获得奖励。

每个 Telegram 账号只能绑定一个 FOGMOE Account；同一个 FOGMOE Account 也不能绑定给多个 Telegram
账号。遇到绑定冲突时不要尝试用相同邮箱或用户名自动合并，应联系管理员核查。

## 解绑与换绑

- 私聊机器人发送 `/fogmoe`，点击“解绑或更换账号”。
- 点击按钮，使用当前已绑定的 FOGMOE Account 再次登录确认。
- 验证的 FOGMOE Account 与当前绑定一致时才会解绑；登录其他账号不会解除当前绑定。
- 解绑后可以重新发送 `/fogmoe` 绑定其他账号。
- 已领取的 20 金币不会扣除，但以后绑定任何账号都不会再次发放奖励。

在 FOGMOE Account 的授权应用页面撤销 OAuth 授权，不会自动删除 Telegram Bot 保存的绑定关系。要更换
Telegram 绑定，仍需通过 `/fogmoe` 中的“解绑或更换账号”完成验证；要同时撤销应用授权，可在解绑后另行
前往授权应用页面操作。

## 安全与隐私

登录和密码输入只发生在 FOGMOE Account 的 OAuth/OIDC 页面，Telegram Bot 不会看到用户密码。Bot 只保存：

- Telegram 用户 ID；
- FOGMOE Account 的稳定账号标识；
- 用于显示的 FOGMOE 用户名缓存；
- 绑定、解绑、首次奖励和最近验证时间。

Bot 会在认证期间取得邮箱权限，但不会将邮箱写入绑定表。
授权码、PKCE verifier、FOGMOE access token、ID token 和 refresh token 不会保存为账号绑定资料。
短时认证事务结束后会被删除。解绑采用软解绑：历史记录和首次奖励领取记录会保留，用来防止换绑后重复领奖。

## 什么时候参考本文

用户问 FOGMOE Account 是什么、怎样绑定或换绑、为什么没有再次获得 20 金币、能否一个账号绑定多处，
或担心 Telegram Bot 会不会看到 FOGMOE 密码时，参考本文。
