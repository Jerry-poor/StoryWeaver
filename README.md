# StoryWeaver

这是一个纯 JSON、纯文件的小说写作 Web 工具，支持：

- 先输入故事描述，再生成大纲草案
- 确认大纲后再进入章节生成
- 维护 `outline.json`
- 维护 `outline_draft.json`
- 维护 `story_brief.json`
- 维护 `characters.json`
- 维护 `storyline.json`
- 维护 `conversation_memory.json`
- 最近 10 轮对话保留
- 旧对话自动压缩成摘要
- 章节生成时强制参考大纲、角色表、故事线和最近对话
- 使用 DeepSeek OpenAI 兼容接口进行章节生成和记忆压缩

## 运行

```bash
cd chat
python app.py
```

浏览器打开：

```text
http://127.0.0.1:8787
```

## 环境变量

- `DEEPSEEK_API_KEY`
- `DEEPSEEK_BASE_URL`，默认 `https://api.deepseek.com/v1`
- `DEEPSEEK_MODEL`，默认 `deepseek-chat`
- `PORT`，默认 `8787`

如果你使用本地的 OpenAI 兼容服务，也可以把 `DEEPSEEK_BASE_URL` 指向本地地址。

## 真实测试

运行一次真实环境的清理 + 生成 1 份大纲 + 5 章测试：

```powershell
cd chat
.\run_real_test.ps1
```

## 数据文件

- `data/outline.json`
- `data/characters.json`
- `data/storyline.json`
- `data/conversation_memory.json`
- `data/chapters/chapter_XXX.json`

## 流程

1. 输入故事描述
2. 生成大纲草案
3. 检查并确认大纲
4. 维护角色表和故事线摘要
5. 通过章节生成接口写章节
6. 每次生成后自动回写：
   - 章节文件
   - 故事线
   - 角色当前状态
   - 最近对话

## 说明

- 不使用数据库
- 不使用向量库
- 不做模糊检索
- 只依赖结构化 JSON 和记忆压缩
