# Model Council

[English](README.md) | 简体中文

一个把其他大模型请上桌的 MCP 服务器。你的助手替你去问它们、把回答当作工具结果读回来、再把彼此的回答转给对方互评，最后给你一个合并结论 —— 全程在同一段对话里，不用复制粘贴。

助手是这个"议会"的主席。成员数量不限，来源可以是任意 OpenAI 兼容或 Anthropic 兼容的端点：官方 API、自建网关、中转站、本地推理服务，混着用也行。

## 工具

| 工具 | 作用 |
|------|------|
| `ask(model, prompt)` | 按 id 问其中一个成员 |
| `ask_all(prompt, models?)` | 并行问全体（或指定的几个），回答并排返回 |
| `list_council()` | 花名册：id、端点、每个成员是否就绪。不发网络请求 |
| `probe_models(model?)` | 打某个端点的 `/models` 路由，看它到底提供哪些模型 id |

成员是无状态的，看不到你的对话，所以主席每次调用都会把需要的信息全部带上。跨模型互评正是靠这一点实现的 —— 把甲的回答塞进乙的 `prompt` 里。

## 安装

服务器直接从 PyPI 运行，不用 clone，也不用建虚拟环境。需要先装 [uv](https://docs.astral.sh/uv/)：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Claude Desktop

编辑 `claude_desktop_config.json`（设置 → 开发者 → 编辑配置），加入下面这段，然后**完全退出并重开**应用 —— Cmd-Q，不是关窗口。工具菜单里出现 `model-council` 就说明成了。

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

## 配置议会

两层结构，好处是多个模型共用一个端点时不必重复填凭据：

- **provider（供应方）** —— 一个端点：`base_url` + `api_key` + 它说哪种协议
- **member（成员）** —— 某个 provider 上的一个具体模型，用一个短 id 称呼

配置优先从 JSON 文件读，找不到就读环境变量。`list_council()` 会明确告诉你实际用的是哪一个，不用猜。

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

每个成员可用：`_BASE_URL`、`_API_KEY`、`_MODEL`、`_FORMAT`、`_LABEL`、`_MAX_TOKENS`、`_TEMPERATURE`、`_TIMEOUT`、`_HEADERS`（JSON 对象）、`_ENABLED`。
全局可用：`COUNCIL_TIMEOUT`、`COUNCIL_CONFIG`、`COUNCIL_ENV_FILE`。

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
    { "id": "gpt5",  "provider": "my-relay", "model": "gpt-5", "label": "GPT-5" },
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
| `format` | provider，或未写 provider 的 member | `openai`（默认）或 `anthropic` |
| `model` | member | 发给端点的模型 id |
| `label` | member | 回答里显示的名字，默认用 id |
| `max_tokens` | member | 仅 anthropic 协议，该协议要求必填。默认 8192 |
| `temperature` | member | 只在设置了的时候才发送 |
| `headers` | provider、member | 额外的 HTTP 头 |
| `timeout` | provider、member | 秒。默认 180 |
| `enabled` | member | `false` 可以临时停用某个成员而不删配置 |

### 协议注意事项

- **`format` 不会从 URL 推断。** 把 `base_url` 指向 Anthropic 风格的端点却没同时设 `format: "anthropic"`，成员仍然停留在 OpenAI 协议上，每次调用都会失败。**这是最常见的配置错误。**
- **Anthropic 端点：** 服务器会拼 `{base_url}/v1/messages`，所以 `base_url` 里不要已经带上 `/v1`。
- **OpenAI 兼容端点：** 服务器只用 `/chat/completions`，不用 `/responses`。有些网关两个都提供，但 `/responses` 可能注入供应方指定的系统人格，对一个通用顾问模型来说是错的。
- **模型 id 变得很快。** 用 `probe_models` 看看某个端点今天到底提供什么。

## 怎么用

值得对主席说的话：

- *"你先自己回答，然后 `ask_all`，给我一个表格列出你们几个在哪里一致、哪里分歧。"*
- *"把这个问题问 gpt5 和 glm，然后点评两边的回答，告诉我哪个更对、为什么。"*
- *"第一轮 `ask_all`。第二轮把其他成员的回答给每一个看，让它修订。然后给我合并后的最终答案。"*
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

## 许可

MIT
