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

### 启动后端服务

您可以使用以下两种方式之一运行服务：

1. **直接启动新后端主模块 (推荐)**：
   ```bash
   python3 backend/main.py
   ```

2. **使用兼容入口启动**：
   ```bash
   python3 app.py
   ```

浏览器打开：

```text
http://127.0.0.1:8787
```

## 项目结构 (前后端分离)

- `/frontend` - 存放独立的前端静态页面 (`index.html`)。
- `/backend` - 存放后端 Python 代码，通过主模块 `main.py` 启动。
  - `backend/app/` - 拆分出的模块化子功能包 (`config`, `storage`, `llm`, `agents`, `pipeline`, `handlers`)。
- `app.py` - 根目录下的兼容性层，动态代理所有的旧方法调用并支持原命令启动。

## 运行单元测试

可以通过以下命令执行所有的章节质量及接续性管道的自动化测试：

```bash
python3 -m unittest test_chapter_quality.py
```

## 环境变量

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
