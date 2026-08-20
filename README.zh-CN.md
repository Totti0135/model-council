# Model Council

[English](README.md) | 简体中文

一个把其他大模型请上桌的 MCP 服务器。你的助手替你去问它们、把回答当作工具结果读回来、再把彼此的回答转给对方互评，最后给你一个合并结论 —— 全程在同一段对话里，不用复制粘贴。

助手是这个"议会"的主席。成员数量不限，来源可以是任意 OpenAI 兼容或 Anthropic 兼容的端点：官方 API、自建网关、中转站、本地推理服务，混着用也行。

## 工具

| 工具 | 作用 |
|------|------|
| `ask(model, prompt, materials?)` | 按 id 问其中一个成员 |
| `ask_all(prompt, models?, rounds?, guests?, materials?, steelman?)` | 并行问全体（或指定的几个），回答并排返回。`rounds=2` 让它变成一场讨论；`steelman` 安排一个每轮都会回嘴的反方席位；`guests` 把你手上已有的回答请进来；`materials` 把要看的材料交给它们 |
| `revise(prompt, answers, round?, materials?)` | 你自己推进一轮：把上一轮所有人说的话（包括只有你能产出的那些）摆给成员看，拿回它们修订后的回答 |
| `revision_prompt(prompt, answers, seat, materials?)` | 生成你自己那一席下一轮该用的 prompt，与成员拿到的逐字一致。不发网络请求 |
| `list_council()` | 花名册：id、端点、权重、每个成员能被喂什么、各自走哪条线路出去、调用预算、是否就绪。不发网络请求 |
| `probe_models(model?)` | 打某个端点的 `/models` 路由，看它到底提供哪些模型 id |

成员是无状态的，看不到你的对话，所以主席每次调用都会把需要的信息全部带上。跨模型互评正是靠这一点实现的 —— 把甲的回答塞进乙的 `prompt` 里。

### 把材料交给议会

问题通常都是**关于**某样东西的 —— 一份方案、一段日志、一个 diff、一张截图。最顺手的做法是把它粘进 `prompt`，而这恰恰是最贵的做法，贵在一个没人盯着的地方：`prompt` 是主席**写出来**的参数，所以一份长文档每次调用都要花掉一整份自己的生成 token，而成员最终读到的，是主席当时复述出来的版本。四十页的东西，那多半已经不是原文了。议会评审的是一份转述，而记录里对此只字不提。

`materials` 换成"指给它看"：

```
ask_all(
  prompt="这套设计在高负载下会从哪里塌？",
  materials=[
    {"label": "设计稿", "path": "/abs/path/design.md"},
    {"label": "P99 曲线", "path": "/abs/path/p99.png"},
    {"label": "我的摘要", "text": "……某些没有对应文件的东西……"},
  ],
  rounds=2,
)
```

服务器负责读取，并把它放在问题前面 —— 对每个成员、每一轮，都放在同一个位置。这个位置不是排版问题：

- **字节完全一致。** 每个成员争论的是同一份副本，它们的分歧才是关于文档本身的，而不是关于各自拿到的是哪一版。
- **前缀可以被缓存。** 材料结束之前的内容，在一场讨论的每次调用里都相同。anthropic 协议会被明确告知这条边界（在那里打一个 `cache_control` 断点）；靠前缀自动缓存的端点，本来就需要这个形状。个别网关不认这个字段，给该成员配 `"cache": false`。
- **主席只付一次。** 一个路径对主席来说就是一行字，不是一整份文档；后续第 2、3 轮由 `ask_all` 自己带过去。

**图片是最值得的一项。** 没有它，主席只能把截图转述成文字 —— 于是每个成员读到的是**同一份转述**，主席看漏的东西全体一起看漏，互评根本没机会纠正。这是唯一一类"传得潦草就会悄悄毁掉议会独立性"的输入。png、jpeg、gif、webp 会发给所有看得见的成员；看不见的成员应当配 `"vision": false`，它会**回避**带图的调用，而不是只拿到文字却装作看过图那样作答。记录里会写明谁回避了这一轮。

**怎么判断哪些成员看不见：** 看不见的成员通常不会说自己看不见。开发这个功能时实测过的一个 anthropic 兼容网关，会照单收下 image 块、然后丢掉，模型接着报了一个颜色 —— 而不是按 prompt 明确要求的那样回答"没有收到图片"。同一个端点上的文字材料却完整送达，所以整次调用从外面看不出任何异常。给某个成员一张一眼能认的图，问它图里是什么；答得笃定又答错，就是这个症状，配上 `"vision": false` 即可。

两个要记住的边界。材料读不出来会**中止整次调用** —— 一个没拿到文档的议会照样会侃侃而谈，读起来和真看过一模一样。另外，`revision_prompt` 只会**点名**你的文件（放在成员看到它的那个位置），不会把内容再吐回来，所以在把 prompt 交给你自己那一席之前，记得先替它打开这些文件。

这个服务器到底读不读路径，取决于它是怎么启动的；HTTP 模式下默认不读，除非部署者[指定了一个目录](#material-over-http)。`list_council` 会在表格上方用一行说明当前是哪种。

### 多轮讨论

`ask_all(prompt, rounds=2)` 把这套互评直接做掉。第一轮就是平常的并行提问；第二轮再去找每个成员，这次把原问题连同第一轮的**全部**回答一起带上 —— 它自己的和别人的，原文照搬 —— 让它据此修订：对的就采纳，错的就改，仍然不同意的地方讲清楚为什么。返回的记录按轮次分块，谁改了口、谁守住了立场，一眼看得见。

**把上一轮带回去，就是这件事的全部机制。** 成员在两次调用之间什么都不记得，不带回去的话，所谓第二轮不过是把同一个问题又问了一遍。最多 3 轮；每多一轮就多一次逐成员的调用，且 prompt 比上一轮更长 —— 想要一份意见普查用 1 轮，问题的价值在于分歧本身时用 2 轮。

**`rounds` 是在什么都还没问之前就定下的**，这是它唯一的毛病：你在没读到第一轮的情况下就为第二轮买了单，而一个最后发现大家意见一致的议会，花的钱和一个还在吵的议会一模一样。想先看看再说，就用 `rounds=1`，读完之后再用 [`revise`](#让-subagent-成为正式一员) 买下一轮 —— 它一次只跑一轮，你觉得值就再来一次，而且它自己没有轮数上限。先读再买通常是更便宜的那一侧：多一轮是每个成员一次完整调用，而 `revise` 只让你付把上一轮回答写回去的成本。`ask_all` 上的 3 是**单次调用**的花费上限，不是这场讨论的上限；真的跑到那里时，记录里会写明这一点。

议会里的成员本来就少有旗鼓相当的，所以还可以给每个成员配[权重](#权重)。

### 常设反方

议会大多数时候是会取得一致的，而这份一致恰恰是它产出的信息量最小的东西。上面所有机制都在**抵抗**趋同——成员彼此匿名、永远不知道权重、收尾指令叫它们别因为人少就让步——但没有任何一样东西在**制造分歧的压力**。一个觉得方案没问题的成员，不会主动把最强的反对意见说出来。

`steelman` 就是给它安排一个席位：

```
ask_all(prompt, rounds=3, steelman={})
```

由一个成员每轮写出针对"桌上正在收敛到的那个结论"的最强反驳，然后作为一个普通的匿名回答摆回给所有人。下一轮它们必须处理它。

**没有任何成员被要求去替一个自己不相信的立场辩护。** 这是分界线，也是它和"正反方辩论模式"的根本区别。成员说的仍然是它们真正想的；指派只存在于一次它们从不知情的额外调用里。给一个席位打上"（被指派反对）"的标签，模型就会去折扣这个论证而不是回答它——所以**来源信息给你**，写在记录末尾的注里；**论证本身给它们**。

**它每一轮都说话，不是只说一次。** 这条看着像细节，其实不是。一个不能回应针对自己的反驳的反对意见，是被**引用**而不是被**代表**：它无法纠正别人对它的误读，于是到第三轮，桌上是在跟自己对它的转述争论，还把这当成回答了它。`tenure` 可以买比默认更少的轮数；席位提前退场时，记录会写明它是**按配置退场的**——因为一段没有解释的沉默，读起来和弃守立场一模一样。

反过来那一面同样重要：反方最后说的那句话就是整份记录的最后一句，没有任何人被要求接招，而记录同样会说明这一点。**没人回答的反对意见不是"站住了"的论点，是"根本没被检验过"的论点。** 用 `rounds=2` 时这对它说的每一句都成立——所以那条注会直接告诉你该改哪个设置，而不是让你付完钱读完了才发现。

把它产出的东西读作"这个议会按需能给出的最强反对"，永远不要读作"有人真的这么认为"。它的某个论点**在被回答之后仍然站得住**，那才有分量；论点刚出现的那一轮，还什么都不值。

默认由本次被问到的第一个成员来写，它同时也以自己的身份作答——就它所知，这两次调用毫无关系。要指定别人用 `steelman={"model": "glm"}`。

### 把你手上已有的回答请进来

主席不只是主持——它自己也能答，在 Claude Code 或 Codex 里还可以起一个 subagent 去答同一个问题。以前这些回答只能**摆在**议会的回答旁边，最后靠你自己人工比对。`guests` 把它们**放进**议会：

```
ask_all(
  prompt="这个方案有什么坑？",
  guests=[{"label": "Subagent", "text": "<你的 subagent 答的正文>"}],
  rounds=2,
)
```

第 1 轮它和成员并排出现。从第 2 轮起，**每个成员都会拿到这段原文，并被要求与它辩论**：

```
--- ANSWER C (does not revise between rounds) ---
迁移脚本没有回滚路径
```

区别全在这里。不传的话，那些模型自始至终不知道你的 subagent 有过意见，你只能自己去做对比——而这件事多轮机制本来就做得更好，因为它让模型互相回应，而不是各说各话。

几点值得知道：

- **传原文，不要传摘要。** 成员看到的就是你传的那段字，摘要不是你想让人评的东西。
- **guest 只发言一次。** 没有谁可以去找它要修订版，所以第 1 轮之后它不再出现。记录里会说明这一点——否则一个中途消失的席位读起来像是弃守了立场。
- **guest 也算一个声音。** 一个成员 + 一个 guest 就够开第 2 轮了——你的 subagent 对一个模型，也是一场讨论。
- **`weight` 对 guest 同样有效**，和成员在同一把尺子上。
- guest 是**逐次调用**的，不写进配置，也不留存。

### 让 subagent 成为正式一员

guest 只说一次。要让一个由你产出的声音**跟上**成员的节奏——作答、读别人的、修订，一轮接一轮，和它们完全一样——就必须由你来推进轮次，因为这个服务器起不了你的 subagent。`revise` 按需跑一轮：

```
ask_all(prompt)                                  → 成员给出第 1 轮回答
  ……你用同一个 prompt 起 subagent                  → 它的第 1 轮回答

revise(prompt, round=1, answers=[
  {"model": "glm", "text": "<glm 第 1 轮的回答>"},
  {"model": "sol", "text": "<sol 第 1 轮的回答>"},
  {"label": "Subagent", "text": "<subagent 第 1 轮的回答>"},
])                                               → 成员带着修订回来
  ……你拿同样的材料把 subagent 再跑一遍

revise(prompt, round=2, answers=[……第 2 轮……])    → 以此类推
```

用 `model` 点名成员，正是"修订"成立的关键：那个成员会拿回**它自己**上一轮的回答，被要求在此基础上移动，而不是从头重答。写 `label` 的条目则是花名册之外的声音。

**成员分辨不出区别。** 传给 `revise` 的外部声音，呈现方式和另一个成员完全一样——一个光秃秃的字母 `--- ROUND-1 ANSWER C ---`——因为它下一轮还会作答；而 `ask_all` 的 guest **会**被标注为已结束，也是同一个道理。把一个马上还要开口的声音描述成已经说完了，是在向正在辩论的模型谎报现场。

**给 subagent 的 prompt 要用 `revision_prompt` 生成，不要拿原问题重问。** 这是这套流程唯一容易搞错的地方，而且它**不会报错**：拿原问题重新问一遍，subagent 只会把上一轮的答案再说一次，记录看上去仍然像一场讨论，没有任何地方标出那一席从哪一轮起就不再参与了。

```
revision_prompt(prompt, answers=[……和 revise 传的一样……], seat="Subagent", round=1)
```

它返回的就是成员拿到的那份：它自己上一轮的回答被标成"你自己的"，别人的答案，以及同一段结尾指令——包括那句"不要因为寡不敌众就放弃你仍然认为正确的立场"。议会之所以不会塌缩成一片附和，多半靠这句话；少了它的那一席，被问的其实不是同一个问题。这个调用是本地的，不发网络请求，所以跟 `revise` 并行发就行，不必等它。

`revise` 不设轮数上限——`ask_all` 那个 3 轮的天花板是因为它自己花预算，而这里每一轮都是你主动付的。

| | `ask_all(guests=…)` | `revise(answers=…)` |
|---|---|---|
| 调用次数 | 一次 | 每轮一次 |
| 你的声音 | 只说一次 | 每轮都修订 |
| 谁推进轮次 | 服务器 | 你 |

### 成员之间是匿名辩论的

在修订轮里，别人的回答是以字母出现的，不带名字：

```
--- ROUND-1 ANSWER B ---
并发写有竞态
```

模型名和权重是同一类信号，而且是**更强**的那一个——模型对彼此的厂牌有很硬的先验。挡住数字却印上品牌，等于关掉小的那条通道、留着大的那条。送到成员面前的只有论证，而论证正是我们想让它评的东西。

字母来自席位在桌上的位置，所以是稳定的：`B` 对每个成员、在每一轮都指同一个席位——这才使得某个成员说「B 关于索引那点错了」，下一轮还能被听懂。

**名字留给你。** 只要记录里发生过修订轮，末尾就会附上对照表：

```
[the members saw each other as letters, not names: A = GLM-5.3, B = GPT-5.6-sol,
C = Subagent. ...]
```

你是唯一的例外，因为需要分辨谁是谁的只有你——谁改了口、谁守住了、谁在说谁。

一个诚实的边界：匿名覆盖的是**服务器写的标签**，不包括模型写在自己回答正文里的名字。那段文字会原样带进下一轮，因为另一种做法是去改别人正要评阅的原文。这里的匿名去掉的是那个常设信号，不是每一次提及。

### 失败重试

会话式失败（HTTP 429、5xx，以及连接中断或超时）会先重试再上报。退避从 1 秒起指数增长并带抖动；端点自己给的 `Retry-After` 优先于这条曲线 —— 除非它要求等待超过 30 秒，那就直接结束并说明，而不是干等在那里。**再试也不会变的失败 —— 401、404、响应体格式不对 —— 立即上报**：重试它们只是花掉同样的额度换来同样的回答。默认重试 2 次；设 `retries: 0` 可回到过去"一次失败即终结"的行为。

某个成员把次数用完仍然失败时，它的错误就作为该成员的"回答"返回，议会其余成员照常作答 —— 除非它还有别的路可走，那就走那条，见[备用端点](#备用端点)。

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
| `--materials-root` | `materials` 路径唯一允许指向的目录；不设它，HTTP 模式一律拒绝路径 |

`--trust-proxy` 值得多看一眼。不设它，`X-Forwarded-For` 会被完全忽略，由对端地址说了算 —— 挂在 nginx 后面时那就是 nginx，白名单要么放行所有人、要么谁都不放行。设了它之后，真实客户端取的是转发链里**最右边那个不是可信代理**的地址，这正是让别人没法靠伪造 `X-Forwarded-For: 10.0.0.1` 直接混进来的原因。

带 `Origin` 头的请求默认一律拒绝。MCP 客户端不是浏览器，不会发这个头；网页则一定会发。白名单放行的是整个办公网，而那上面每台机器都跑着浏览器，浏览器会替它当前打开的任意页面发请求 —— `Origin` 就是区分这两者的依据。

每个参数都有对应的环境变量（`COUNCIL_ALLOW`、`COUNCIL_TRUST_PROXY`、`COUNCIL_HTTP_HOST`……），`--help` 里列全了。

<a name="material-over-http"></a>

### HTTP 模式下的材料

stdio 模式下，`materials` 能读进程能读的任何文件，且不需要任何开关：能拉起这个进程的人本来就有这个权限 —— 和 loopback 绑定不需要 `--allow` 是同一个道理。HTTP 模式下这个前提消失了：调用方是任何能连上端口的人，一个路径就会把共享议会变成读取宿主机文件的通道，而读到的内容还会发往外部 provider。所以 HTTP 部署**一律拒绝路径**，调用方改用 `text` 把内容带进来。

`--materials-root /srv/council/material` 只打开一个目录，比较之前会先解析真实路径，所以软链接按它指向的位置判定。

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

每个成员可用：`_BASE_URL`、`_API_KEY`、`_MODEL`、`_FORMAT`、`_LABEL`、`_WEIGHT`、`_MAX_TOKENS`、`_TEMPERATURE`、`_TIMEOUT`、`_RETRIES`、`_RETRY_BACKOFF`、`_HEADERS`（JSON 对象）、`_PROXY`、`_VISION`、`_CACHE`、`_ENABLED`。
全局可用：`COUNCIL_TIMEOUT`、`COUNCIL_RETRIES`、`COUNCIL_RETRY_BACKOFF`、`COUNCIL_PROXY`、`COUNCIL_CONFIG`、`COUNCIL_ENV_FILE`、`COUNCIL_MATERIALS_ROOT`。

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
| `format` | provider，或未写 provider 的 member | `openai`（默认）或 `anthropic` |
| `model` | member | 发给端点的模型 id |
| `label` | member | 回答里显示的名字，默认用 id |
| `weight` | member | 这个成员的意见有多重。默认 1，上限 10，`0` 表示只供参考。见[权重](#权重) |
| `max_tokens` | member | 仅 anthropic 协议，该协议要求必填。默认 8192 |
| `temperature` | member | 只在设置了的时候才发送 |
| `headers` | provider、member | 额外的 HTTP 头 |
| `timeout` | provider、member | 秒，单次尝试的上限。默认 180 |
| `retries` | provider、member | 会话式失败额外可重试的次数。默认 2，上限 5，填 `0` 关闭 |
| `retry_backoff` | provider、member | 第一次重试前等待的秒数，之后翻倍。默认 1 |
| `proxy` | 顶层、provider、member | 出网线路。省略则跟随 `HTTP_PROXY`/`HTTPS_PROXY`；`false` 表示直连；填 URL 则走该代理；填 `"env"` 表示回到环境里的代理。见[代理](#代理) |
| `vision` | member | 模型看不了图就填 `false`。它会回避带图的调用，而不是只拿到文字照样作答。默认 `true` |
| `cache` | provider、member | 填 `false` 就不再随材料发送 `cache_control` 断点。仅 anthropic 协议；网关不认这个字段时关掉它。默认 `true` |
| `enabled` | member | `false` 可以临时停用某个成员而不删配置 |
| `backups` | member | 同一个席位的备用端点，按顺序在前一个不通时接手。最多 4 个。见[备用端点](#备用端点) |

`timeout`、`retries`、`retry_backoff`、`proxy` 也可以写在配置文件的顶层，作为所有成员继承的默认值。

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

### 备用端点

同一个模型常常有不止一条路可走 —— 公司网关、公网中转、另一把备用 key。把它们各自配成一个 member 是错的形状：那等于把同一个模型请进屋里好几遍，于是它答好几次、被称好几次重量，第二轮还要跟自己争。

`backups` 是**同一个席位**的备用端点，前一个不通时按顺序往下试：

```json
{
  "id": "sol", "provider": "internal", "model": "gpt-5-codex", "label": "Codex",
  "backups": [
    { "provider": "my-relay" },
    { "base_url": "https://another-relay.example/v1", "api_key": "${SPARE_KEY}",
      "model": "gpt-5-codex-latest" }
  ]
}
```

一个 id、一个 label、一份权重、一票。**你不会直接点名某个备用端点** —— `ask(model="sol")` 会落到三条里当时通的那条上。

备用端点会继承席位在"模型"这件事上定下来的一切 —— `model`、`max_tokens`、`temperature`、`vision`、`timeout`、`retries` —— 所以最常见的情况（同一个模型换一个中转）就是上面那一行。只有当对面那条通道给它起了别的名字时，才需要写 `model`。有三样东西永远不继承：

- **连接本身**（`base_url`、`api_key`、`format`）—— 那正是备用端点存在的理由。它要么写 `provider` 从中整份取用，要么自带完整的三项。各取一半会被拒绝，理由见[用配置文件](#用配置文件)。
- **`headers`** —— 为某个端点写的 header，往往本身就是给那个端点的凭据。
- **`proxy`** —— 线路属于主机，不属于席位。一个为内网网关钉死 `proxy: false` 的席位，否则会把它的公网备用端点也一起送出代理之外，而那些恰恰是需要代理的。备用端点的线路来自它自己的 provider，或者议会的默认值。

**什么时候会切换。** 不只是连不上：key 被吊销、中转不再挂那个模型 id、网关返回 `200` 但内容不是回答 —— 从席位的角度看这些是同一件事：**这条路现在不通向那个模型，而配置里还写着另一条**。会话式失败（见[失败重试](#失败重试)）仍然先在出问题的那条连接上重试，所以备用端点是给**挂掉**的端点准备的，不是给答得慢的。想让它别等、直接切，就给主端点写 `retries: 0`。

**是哪个端点答的，会印在回答上面。** 备用端点常常是另一个模型 id，有时干脆是另一个模型 —— 而一份被当作"模型之间的对比"来读的记录，绝不能悄悄换掉对比的对象：

```
===== Codex (gpt-5-codex-latest — backup 2, after the primary did not answer) =====
```

`list_council` 会把这条链缩进显示在席位下面：

```
id            label        model        format  route   endpoint                       tries     status
sol           Codex        gpt-5-codex  openai  direct  https://gateway.internal/v1    3 × 180s  ready
  ↳ backup 1               gpt-5-codex  openai  env     https://your-relay/v1          3 × 180s  standing by
  ↳ backup 2               gpt-5-c...   openai  env     https://another-relay/v1       3 × 180s  standing by
```

`probe_models` 会把每一环都探一遍，这样某个备用端点悄悄不再挂那个模型时，你会在**真正需要它的那天之前**就看见。整条链都没答上来的席位，会把它试过的每一个连接和各自的原因一并报出来。

备用端点只在配置文件里有；环境变量那套 roster 没有对应写法。每个席位最多 4 个 —— 席位是一个模型的一份意见，不是高可用集群，而且 `ask_all` 要等最慢的那条链把失败走完。

### 代理

所有成员默认跟随这台机器的 `HTTP_PROXY`/`HTTPS_PROXY`。这个默认值一直是对的，直到议会同时坐着两类端点：一类只有走代理才通，另一类（典型是内网网关）恰恰是代理到不了的。一个开关不可能同时对这两边都对，所以**线路是按席位分别决定的**。

`proxy` 有四种取值，可以写在 member 上、写在 provider 上，也可以写在配置文件顶层作为整个议会的默认：

| 取值 | 走哪条线路 |
|------|-----------|
| 省略 | 有议会级 `proxy` 就跟随它，否则跟随 `HTTP_PROXY`/`HTTPS_PROXY` |
| `false` 或 `"direct"` | 直连，两者都不理会 |
| 一个 URL | 走这个代理 —— 支持 `http://`、`https://`、`socks5://`、`socks5h://` |
| `"env"` | 回到 `HTTP_PROXY`/`HTTPS_PROXY`，用于整个议会已被指到别处、而这一席要留在环境代理上的情况 |

**越具体越优先**：member → provider → 议会默认 → 环境变量。所以"大部分走代理、有两席不能走"，就是先说一次、再说两次：

```json
{
  "proxy": "http://127.0.0.1:7890",
  "providers": {
    "internal": { "base_url": "https://gateway.internal.example/v1",
                  "api_key": "${INTERNAL_KEY}", "proxy": false }
  },
  "members": [
    { "id": "gpt5",  "provider": "my-relay", "model": "gpt-5" },
    { "id": "inhouse", "provider": "internal", "model": "some-internal-model" },
    { "id": "local", "base_url": "http://127.0.0.1:11434/v1", "api_key": "-",
      "model": "qwen3-8b", "proxy": false }
  ]
}
```

用环境变量是同样三步 —— `COUNCIL_PROXY` 管整个议会，`<ID>_PROXY` 管某一个成员：

```bash
COUNCIL_PROXY=http://127.0.0.1:7890
INHOUSE_PROXY=direct
LOCAL_PROXY=direct
```

**`list_council` 会打印每个成员最终走的线路** —— `env`、`direct` 或代理 URL。这一列只在成员之间可能不同的时候才出现：

```
network: HTTPS_PROXY=http://127.0.0.1:7890 in this server's environment — the members whose route is 'env' go through it

id       label    model      weight  format  sees      route                  endpoint                          tries     status
gpt5     GPT-5    gpt-5      1       openai  text+img  env                    https://your-host/v1              3 × 180s  ready
inhouse  Inhouse  internal   1       openai  text+img  direct                 https://gateway.internal/v1       3 × 180s  ready
kimi     Kimi     kimi-k2    1       openai  text+img  http://127.0.0.1:7890  https://api.moonshot.cn/v1        3 × 180s  ready
```

代理 URL 里如果带密码，凡是打印出来的地方都会打码 —— 表格、警告、连接失败的报错文本。

**填错的代理在读花名册时就会被发现**，不必等到第一次调用。少写协议头的 `127.0.0.1:7890` 会被读成 `http://127.0.0.1:7890` 并在警告里说明；而一个根本拨不出去的协议头会让那个成员被停用，理由就写在 `list_council` 里，而不是让它每次调用都从请求内部抛一个 `ValueError`。选择停用而不是悄悄改走别的线路，是因为：既然特意指定了代理，就是希望流量从那里走。

`socks5://` 需要一个 httpx 默认不装的包。装上附加依赖 —— `pip install 'model-council-mcp[socks]'`，或 `uvx --from 'model-council-mcp[socks]' model-council-mcp` —— 否则那个成员会被停用，并附上说明这件事的提示。

### 协议注意事项

- **`format` 不会从 URL 推断。** 把 `base_url` 指向 Anthropic 风格的端点却没同时设 `format: "anthropic"`，成员仍然停留在 OpenAI 协议上，每次调用都会失败。**这是最常见的配置错误。**
- **Anthropic 端点：** 服务器会拼 `{base_url}/v1/messages`，所以 `base_url` 里不要已经带上 `/v1`。
- **OpenAI 兼容端点：** 服务器只用 `/chat/completions`，不用 `/responses`。有些网关两个都提供，但 `/responses` 可能注入供应方指定的系统人格，对一个通用顾问模型来说是错的。
- **系统代理默认会被沿用。** 如果某个成员在代理到不了的网络上（典型是内网网关），它会以一个光秃秃的 `ConnectError` 失败，字面上完全看不出跟代理有关。给那个成员或 provider 加 `"proxy": false` 就直连，其余成员照旧走代理，详见[代理](#代理)。连接失败的报错会点明这个成员当时走的是哪条线路，三种策略不会都失败成同一副样子。
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
- **只有某一个成员报光秃秃的 `ConnectError`，其他都正常** —— 多半是线路问题，不是端点问题。报错会点明那个成员当时走的是哪条线路，`list_council` 的 `route` 一列可以一眼看全，改法见[代理](#代理)。

## 许可

MIT
