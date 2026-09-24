# Reader Core 架构

## 数据流

```text
手动输入 / UTF-8 TXT
  → TextDocument
  → segmentDocument() → AudioSegment[]
  → ReaderQueue
  → POST /v1/audio/speech → WAV Blob
  → AudioPlayer
```

`TextDocument` 的形状固定为 `{ title, chapters: [{ title, paragraphs: [string] }] }`。手动输入形成一个“正文”章节；TXT 以空行分段，并识别独立成段的“第 N 章”等中文标题及 `Chapter N` 英文标题。无法识别的标题仍作为普通文本保留。

`AudioSegment` 保存 `chapterIndex`、`chapterTitle`、`paragraphIndex`、`originalText`、`start`、`end` 和送往 TTS 的 `text`。同一段落的各片段按顺序拼接可还原原段落；章节边界不会被合并。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `sources.js` | 将手动输入或 UTF-8 TXT 转换为 TextDocument；拒绝无效 UTF-8 文件。 |
| `segmenter.js` | 按句末标点和目标长度切分，优先保留引号、括号、句子及章节关系。 |
| `queue.js` | 管理 `idle / generating / playing / paused / stopped / finished` 状态、当前片段和一个预取片段；停止时取消请求并清空音频。 |
| `player.js` | 独立控制浏览器音频的播放、暂停、继续、停止与播放结束回调，并释放 Object URL。 |
| `reader.js` | 连接 UI、各模块与语音 HTTP 请求；UI 不直接控制 audio 元素。 |
| `index.html` | 页面结构和样式。 |

队列播放第 N 个片段时仅请求第 N+1 个片段。预取完成后只保存一个 Blob；播放结束后消费它并开始下一次预取。不保存历史音频。若当前片段播放完而预取尚未结束，状态转为 `generating`，完成后继续播放。

## API 关系

Reader 调用现有 `GET /v1/voices` 获取角色，再以 `{ voice, input, response_format: "wav", speed }` 调用 `POST /v1/audio/speech`。服务层通过 `/test` 返回页面，通过 `/reader-assets/` 提供 ES 模块静态资源。语音 API 的路径及请求、响应格式没有变化。

## 后续 EPUB 扩展

未来的 EPUB 解析器只需在 sources 层输出相同的 TextDocument，并将章节及段落顺序保留。切分器、队列、播放器和语音 API 均无需了解 EPUB 的文件格式。当前版本不读取 EPUB。

## 当前边界

- TXT 使用严格 UTF-8 解码；其他编码需先转换。
- 标题识别使用简单规则，不能保证识别所有书籍排版。
- 超长且完全无安全断点的句子可超过目标长度，以免切断引号、括号或单词。
- 浏览器刷新后不恢复阅读进度。
- 单段预取不能消除生成时间超过剩余播放时间时的等待。
