# Model Council

[English](README.md) | 简体中文

一个把其他大模型请上桌的 MCP 服务器。你的助手替你去问它们、把回答当作工具结果读回来、再把彼此的回答转给对方互评，最后给你一个合并结论 —— 全程在同一段对话里，不用复制粘贴。

助手是这个"议会"的主席。成员数量不限，来源可以是任意 OpenAI 兼容或 Anthropic 兼容的端点：官方 API、自建网关、中转站、本地推理服务，混着用也行。

## 工具

| 工具 | 作用 |
|------|------|
| `ask(model, prompt)` | 按 id 问其中一个成员 |
| `ask_all(prompt, models?, rounds?)` | 并行问全体（或指定的几个），回答并排返回。`rounds=2` 让它变成一场讨论 |
| `list_council()` | 花名册：id、端点、权重、调用预算、每个成员是否就绪。不发网络请求 |
| `probe_models(model?)` | 打某个端点的 `/models` 路由，看它到底提供哪些模型 id |

成员是无状态的，看不到你的对话，所以主席每次调用都会把需要的信息全部带上。跨模型互评正是靠这一点实现的 —— 把甲的回答塞进乙的 `prompt` 里。

### 多轮讨论

`ask_all(prompt, rounds=2)` 把这套互评直接做掉。第一轮就是平常的并行提问；第二轮再去找每个成员，这次把原问题连同第一轮的**全部**回答一起带上 —— 它自己的和别人的，原文照搬 —— 让它据此修订：对的就采纳，错的就改，仍然不同意的地方讲清楚为什么。返回的记录按轮次分块，谁改了口、谁守住了立场，一眼看得见。

**把上一轮带回去，就是这件事的全部机制。** 成员在两次调用之间什么都不记得，不带回去的话，所谓第二轮不过是把同一个问题又问了一遍。最多 3 轮；每多一轮就多一次逐成员的调用，且 prompt 比上一轮更长 —— 想要一份意见普查用 1 轮，问题的价值在于分歧本身时用 2 轮。

议会里的成员本来就少有旗鼓相当的，所以还可以给每个成员配[权重](#权重)；另外也可以让[客户端自己的模型](#给自己的模型留一个席位)占一个席位，而不是接一个端点。

### 失败重试

会话式失败（HTTP 429、5xx，以及连接中断或超时）会先重试再上报。退避从 1 秒起指数增长并带抖动；端点自己给的 `Retry-After` 优先于这条曲线 —— 除非它要求等待超过 30 秒，那就直接结束并说明，而不是干等在那里。**再试也不会变的失败 —— 401、404、响应体格式不对 —— 立即上报**：重试它们只是花掉同样的额度换来同样的回答。默认重试 2 次；设 `retries: 0` 可回到过去"一次失败即终结"的行为。

某个成员把次数用完仍然失败时，它的错误就作为该成员的"回答"返回，议会其余成员照常作答。

## 安装

本服务器已上架官方 [MCP Registry](https://registry.modelcontextprotocol.io)，名字是 `io.github.Totti0135/model-council` —— 支持浏览注册表的客户端可以直接在里面找到并添加。想手动配置就接着往下看。

服务器直接从 PyPI 运行，不用 clone，也不用建虚拟环境。需要先装 [uv](https://docs.astral.sh/uv/)：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Claude Desktop

最省事的是桌面扩展。从 [最新 release](https://github.com/Totti0135/model-council/releases/latest) 下载 `model-council-<版本>.mcpb`，拖到 设置 → 扩展 里即可。应用会用表单向你索取端点和密钥，并把密钥存进系统钥匙串 —— 磁盘上不会有任何文件保存它们。表单提供两个座位；想要更大的议会，把表单里的 "Config file" 指向一份 JSON 配置（见下文）。

想手动配置就编辑 `claude_desktop_config.json`（设置 → 开发者 → 编辑配置），加入下面这段，然后**完全退出并重开**应用 —— Cmd-Q，不是关窗口。工具菜单里出现 `model-council` 就说明成了。

```json
{
  "mcpServers": {
    "model-council": {
      "command": "uvx",
      "args": ["model-council-mcp"],
      "env": {
        "COUNCIL_MODELS": "gpt5,glm",
        "GPT5_BASE_URL": "https://你的-openai-兼容端点/v1",
        "GPT5_API_KEY": "sk-xxxxxxxx",
        "GPT5_MODEL": "gpt-5",
        "GLM_BASE_URL": "https://open.bigmodel.cn/api/anthropic",
        "GLM_API_KEY": "xxxxxxxx",
        "GLM_MODEL": "glm-4.6",
        "GLM_FORMAT": "anthropic"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add model-council -e GPT5_BASE_URL=... -e GPT5_API_KEY=... -- uvx model-council-mcp
```

### 其他 MCP 客户端

任何能启动 stdio 服务器的客户端都行：运行 `uvx model-council-mcp`，传同样的环境变量。

## 用 HTTP 服务整个团队

上面所有装法都是每人一份：由各自的 MCP 客户端拉起进程，各自填自己的 key。`--http` 是另一种形态 —— 一份部署持有一套 key，服务整个团队，同事那边配一个 URL，一个秘密都不用知道。

```bash
model-council-mcp --http --host 0.0.0.0 --allow 10.20.0.0/16
```

同事把它当远程服务器加上，配置里没有任何敏感内容：

```bash
claude mcp add --transport http model-council http://council.internal:8000/mcp
```

```jsonc
// Claude Desktop 和其他客户端
{ "mcpServers": { "model-council": { "type": "http", "url": "http://council.internal:8000/mcp" } } }
```

`deploy/` 里有 [Dockerfile](deploy/Dockerfile)、[compose 文件](deploy/compose.yaml)和 [systemd unit](deploy/model-council.service)。

### 谁可以调用

stdio 模式下这个问题由操作系统回答：能拉起进程的人本来就有 key。HTTP 把这个前提拿掉了 —— 现在是一个端口挡在共享额度前面 —— 所以绑定非 loopback 地址时，没有 `--allow` 说明谁能访问，服务器会**拒绝启动**。loopback 不需要这个参数，永远放行。

| 参数 | |
|------|---|
| `--allow` | CIDR、单个地址，或者别名 `private`（RFC1918 全部）、`loopback`、`any` |
| `--trust-proxy` | 哪些反向代理的 `X-Forwarded-For` 可以采信 |
| `--allow-origin` | 放行某个浏览器 `Origin`，可重复 |

`--trust-proxy` 值得多看一眼。不设它，`X-Forwarded-For` 会被完全忽略，由对端地址说了算 —— 挂在 nginx 后面时那就是 nginx，白名单要么放行所有人、要么谁都不放行。设了它之后，真实客户端取的是转发链里**最右边那个不是可信代理**的地址，这正是让别人没法靠伪造 `X-Forwarded-For: 10.0.0.1` 直接混进来的原因。

带 `Origin` 头的请求默认一律拒绝。MCP 客户端不是浏览器，不会发这个头；网页则一定会发。白名单放行的是整个办公网，而那上面每台机器都跑着浏览器，浏览器会替它当前打开的任意页面发请求 —— `Origin` 就是区分这两者的依据。

每个参数都有对应的环境变量（`COUNCIL_ALLOW`、`COUNCIL_TRUST_PROXY`、`COUNCIL_HTTP_HOST`……），`--help` 里列全了。

### 它做不到什么

白名单是网络边界，不是身份。边界内的人不带任何凭据就能调用，所以用量无法归属到具体某个人，无法按人限流，也无法只吊销某一个人。这是有意的取舍 —— 正因如此同事那边的配置才能只是一个裸 URL —— 但代价是：这个网络必须是你真的信得过的边界。一个放外包进来的 VPN，或者同网段的 CI runner，都会让这个假设失效。

如果你需要按人归属或者按人配额，在这个服务后面放一个 LLM 网关（LiteLLM、one-api，或者公司现成的那套），在网关上给每个调用方发各自的虚拟 key；或者把它挂在做 SSO 的反向代理后面。

还有一件事，在你把 URL 发出去之前值得知道：这个服务会把收到的任意文本转发给外部厂商。一个不需要凭据的共享端点，对所有能访问到它的人来说都是一条数据出境通道。

### 挂在反向代理后面

`ask_all` 配 `rounds=2` 是个长请求 —— 多个模型、每个可能重试多次、每次调用最长 `COUNCIL_TIMEOUT`（180 秒）。代理的默认超时会在服务器干完活之前就把连接掐掉：

```nginx
location /mcp {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_buffering off;          # 这个传输是流式的，开缓冲等于白搭
    proxy_read_timeout 900s;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

然后启动服务器时把 `--trust-proxy` 设成代理自己的地址，否则白名单永远只能看到代理。

## 配置议会

两层结构，好处是多个模型共用一个端点时不必重复填凭据：

- **provider（供应方）** —— 一个端点：`base_url` + `api_key` + 它说哪种协议
- **member（成员）** —— 某个 provider 上的一个具体模型，用一个短 id 称呼

配置取自"最显式"的那个来源：`COUNCIL_CONFIG` 指向的文件 → `COUNCIL_MODELS` 声明的花名册 → `~/.config/model-council/config.json` → 内置默认花名册。显式压过自动发现是有意为之 —— 一份你随手留下的配置文件，不该静默覆盖客户端刚刚传进来的设置。`list_council()` 会明确告诉你实际用的是哪一个，不用猜。

### 用环境变量

`COUNCIL_MODELS` 列出所有 id；每个 id 有一组以它命名的变量，规则是转大写、非字母数字换成下划线（`my-model` → `MY_MODEL_BASE_URL`）。

```bash
COUNCIL_MODELS=gpt5,glm
GPT5_BASE_URL=https://你的-openai-兼容端点/v1
GPT5_API_KEY=sk-xxxxxxxx
GPT5_MODEL=gpt-5
GLM_BASE_URL=https://open.bigmodel.cn/api/anthropic
GLM_API_KEY=xxxxxxxx
GLM_MODEL=glm-4.6
GLM_FORMAT=anthropic
```

每个成员可用：`_BASE_URL`、`_API_KEY`、`_MODEL`、`_FORMAT`、`_LABEL`、`_WEIGHT`、`_MAX_TOKENS`、`_TEMPERATURE`、`_TIMEOUT`、`_RETRIES`、`_RETRY_BACKOFF`、`_HEADERS`（JSON 对象）、`_PROXY`、`_ENABLED`。
全局可用：`COUNCIL_TIMEOUT`、`COUNCIL_RETRIES`、`COUNCIL_RETRY_BACKOFF`、`COUNCIL_CONFIG`、`COUNCIL_ENV_FILE`。

不设 `COUNCIL_MODELS` 时，花名册默认是 `chatgpt,glm`，分别读 `CHATGPT_*` 和 `GLM_*`。

### 用配置文件

成员多起来、或者几个成员共用一个端点时更合适。设 `COUNCIL_CONFIG=/路径/config.json`，或者把文件放到 `~/.config/model-council/config.json`，服务器会自己找到。

```json
{
  "providers": {
    "my-relay": {
      "base_url": "https://你的-openai-兼容端点/v1",
      "api_key": "${MY_RELAY_KEY}",
      "format": "openai"
    },
    "zhipu": {
      "base_url": "https://open.bigmodel.cn/api/anthropic",
      "api_key": "${GLM_KEY}",
      "format": "anthropic"
    }
  },
  "members": [
    { "id": "gpt5",  "provider": "my-relay", "model": "gpt-5", "label": "GPT-5",
      "weight": 2 },
    { "id": "codex", "provider": "my-relay", "model": "gpt-5-codex", "temperature": 0.2 },
    { "id": "glm",   "provider": "zhipu",    "model": "glm-4.6" },
    { "id": "kimi",  "base_url": "https://api.moonshot.cn/v1",
      "api_key": "${KIMI_KEY}", "model": "kimi-k2" }
  ]
}
```

`${ENV_VAR}` 会从环境变量展开，所以这个文件本身不含密钥，可以放心分享或提交。完整带注释的版本见 [examples/config.json](examples/config.json)。

成员获得连接只有两条路，不能混用：要么写 `provider` 整份继承那个端点，要么不写 `provider` 而自己给全 `base_url` + `api_key` + `format`（见上面的 `kimi`）。**写了 `provider` 又覆盖这三者之一会被拒绝** —— 该成员被禁用，`list_council` 会说明原因。理由是：部分覆盖会把一个端点的凭据配上另一个端点的地址，静默地把你的密钥发往一个从未为它签发的主机。成员级的 `headers`、`timeout`、`temperature`、`max_tokens`、`label` 不属于这个身份，照旧可以覆盖。

### 字段

前三个字段作为一个整体存在，见上面的规则。

| 字段 | 适用于 | 说明 |
|------|--------|------|
| `base_url` | provider，或未写 provider 的 member | 路由挂载的根地址 —— openai 协议拼 `/chat/completions`，anthropic 协议拼 `/v1/messages`。OpenAI 兼容端点通常以 `/v1` 结尾 |
| `api_key` | provider，或未写 provider 的 member | |
| `format` | provider，或未写 provider 的 member | `openai`（默认）、`anthropic`，或 `sampling` —— 见[给自己的模型留一个席位](#给自己的模型留一个席位) |
| `model` | member | 发给端点的模型 id |
| `label` | member | 回答里显示的名字，默认用 id |
| `weight` | member | 这个成员的意见有多重。默认 1，上限 10，`0` 表示只供参考。见[权重](#权重) |
| `max_tokens` | member | 仅 anthropic 协议，该协议要求必填。默认 8192 |
| `temperature` | member | 只在设置了的时候才发送 |
| `headers` | provider、member | 额外的 HTTP 头 |
| `timeout` | provider、member | 秒，单次尝试的上限。默认 180 |
| `retries` | provider、member | 会话式失败额外可重试的次数。默认 2，上限 5，填 `0` 关闭 |
| `retry_backoff` | provider、member | 第一次重试前等待的秒数，之后翻倍。默认 1 |
| `proxy` | provider、member | 省略则跟随 `HTTP_PROXY`/`HTTPS_PROXY`；`false` 表示直连；填 URL 则走该代理 |
| `enabled` | member | `false` 可以临时停用某个成员而不删配置 |

`timeout`、`retries`、`retry_backoff` 也可以写在配置文件的顶层，作为所有成员继承的默认值。

### 权重

议会里的成员本来就少有旗鼓相当的。`weight` 表示某个成员的意见有多重 —— 不写就都是 `1`，而且只有比值有意义，所以 `2` 和 `1` 与 `10` 和 `5` 是同一个议会。

```json
{ "id": "gpt5", "provider": "my-relay", "model": "gpt-5", "weight": 2 }
```

它不改变任何调用行为。**只有当权重确实不一致时**，`ask_all` 才会在每条回答上标出它的权重，并在记录末尾附上排名：

```
===== GPT-5 (gpt-5) · weight 2 =====
...
===== Local (qwen3-8b) · weight 0.5 =====
...

[WEIGHTS — GPT-5 2, GLM 1, Local 0.5]
These are this council's standing priors on its members, not votes. ...
```

权重属于"席位"而不属于端点，所以 provider 不能设它：同一个中转站上的两个成员，可能一个是前沿模型、一个是小快模型。`0` 表示只供参考 —— 该成员照样作答、照样被读到，但它的赞同不计入任何支持。填了不能用的值（负数、一个词）会回落到 `1`，而不是悄悄把排名搞坏；`list_council` 会显示实际生效的值。

**成员之间永远看不到彼此的权重。** 一个被告知"你不如别人"的模型会停止争辩、转而附和，而这恰好会赔掉组建议会本来要的那份独立异见 —— 所以第二轮带回去的是别人的回答，不是别人的分量。权重是给读这份记录的人看的，而且它是**先验，不是选票**：它用来打破平局、决定谁负举证责任。**来自最低权重的一条具体的、可核验的理由，依然胜过最高权重的一句空断言。**

### 给自己的模型留一个席位

`format` 设成 `sampling` 的成员没有端点也没有密钥，它由 MCP 客户端自己回答：服务器沿着已经打开的会话发一个 `sampling/createMessage` 请求回去，客户端用它自己的模型跑这个 prompt。

```json
{ "id": "sub", "format": "sampling", "label": "Subagent" }
```

配置就这么多——`base_url`、`api_key`、`model` 全是可选的，`model` 也只是给客户端的一个提示，它可以不理。这个席位不花密钥也不花额度，并且和别的成员一样参与多轮讨论。

**但它不是一个外部意见。** 回答来自一个不带你这段对话的全新实例，所以它确实是"再看一遍"——可它和正在读这份记录、正在写结论的，是同一个模型。只要它真的作答了，`ask_all` 就会在末尾挑明这一点：

```
===== Subagent (this client) =====
...

[Subagent was answered by your own model, over a sampling request back to you: ...
Read it as a second look, not a second opinion — ...]
```

入座前有两条限制值得先知道：

- **大多数客户端不支持 sampling。** 它是可选能力，没有声明这个能力的客户端会收到一句明确的说明，而议会其余成员照常作答。
- **它在 `--http` 下无法工作。** sampling 是一个往客户端方向走的请求，而本服务器跑的是无状态 HTTP，没有这样的回程通道。启动时会有警告。这类席位请放在 stdio 上。

> **关于弃用。** MCP 在 `2026-07-28` 版规范中弃用了 Sampling（[SEP-2577](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2577)），移除前至少保留十二个月，并建议新实现"直接对接 LLM 提供商的 API"——而这正是这里其余成员本来就在做的事。这个席位是**有意**踩在这段窗口期里的：它是唯一能在不加第二把密钥的前提下让客户端自己的模型入座的办法，而替代方案（MRTR）会在工具函数体执行前把请求一次性解析掉，多轮议会没法用那个形状表达。哪天它被移除，坏掉的只有这个席位，别的都不受影响。

### 协议注意事项

- **`format` 不会从 URL 推断。** 把 `base_url` 指向 Anthropic 风格的端点却没同时设 `format: "anthropic"`，成员仍然停留在 OpenAI 协议上，每次调用都会失败。**这是最常见的配置错误。**
- **Anthropic 端点：** 服务器会拼 `{base_url}/v1/messages`，所以 `base_url` 里不要已经带上 `/v1`。
- **OpenAI 兼容端点：** 服务器只用 `/chat/completions`，不用 `/responses`。有些网关两个都提供，但 `/responses` 可能注入供应方指定的系统人格，对一个通用顾问模型来说是错的。
- **系统代理默认会被沿用。** 如果某个成员在代理到不了的网络上（典型是内网网关），它会以一个光秃秃的 `ConnectError` 失败，字面上完全看不出跟代理有关。给那个成员或 provider 加 `"proxy": false` 就直连，其余成员照旧走代理。当环境里确实有代理时，错误信息也会主动提示这一点。
- **模型 id 变得很快。** 用 `probe_models` 看看某个端点今天到底提供什么。

## 怎么用

值得对主席说的话：

- *"你先自己回答，然后 `ask_all`，给我一个表格列出你们几个在哪里一致、哪里分歧。"*
- *"把这个问题问 gpt5 和 glm，然后点评两边的回答，告诉我哪个更对、为什么。"*
- *"这个问题跑两轮 `ask_all`，然后告诉我谁改了口、真正让它改口的是什么。"*
- *"只问 glm —— 这个文件我想要个第二意见。"*

## 本地开发

```bash
uv sync
```

把 `.env.example` 复制成 `.env` 填上真实值，然后：

```bash
uv run python tests/test_smoke.py
```

冒烟测试会离线校验两条配置路径；如果 `.env` 里有可用凭据，最后会做一次真实调用。想让客户端指向你的工作副本，把 `uvx` 换成你环境里的 `model-council-mcp` 可执行文件即可。

## 排查

- **服务器没出现** —— 看客户端的 MCP 日志（Claude Desktop 在 `~/Library/Logs/Claude/mcp*.log`）。服务器启动时会把配置警告写到 stderr。
- **工具返回 `[... is not configured]`** —— 该成员缺 `base_url`、`api_key` 或 `model`。跑 `list_council` 看逐成员的明细。
- **HTTP 401** —— key 不对，或者被供应方禁用了。
- **HTTP 404** —— `base_url` 不对，或者该端点的 `format` 设错了。
- **模型 id 被拒** —— 跑 `probe_models`。
- **某个成员回了 `gave up after N attempts`** —— 它每一次都失败了，错误文本是端点最后一次给的那条。`list_council` 会以 `尝试次数 × 超时` 的形式显示每个成员的预算。
- **一次调用比超时时间长得多** —— 重试是乘上去的：3 次尝试、每次 180 秒，最坏情况约 9 分钟，还要加上退避等待。希望某个成员"快速失败"的话，把它的 `timeout` 或 `retries` 调小。

## 许可

MIT
