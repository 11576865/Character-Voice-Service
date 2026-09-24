import { documentFromManual, readTxtFile } from "./sources.js";
import { segmentDocument } from "./segmenter.js";
import { AudioPlayer } from "./player.js";
import { ReaderQueue } from "./queue.js";

const voiceSelect = document.getElementById("voice");
const speedInput = document.getElementById("speed");
const textInput = document.getElementById("text");
const fileInput = document.getElementById("txtFile");
const startButton = document.getElementById("start");
const pauseButton = document.getElementById("pause");
const stopButton = document.getElementById("stop");
const statusBox = document.getElementById("status");
const sourceBox = document.getElementById("source");
const chapterBox = document.getElementById("currentChapter");
const paragraphBox = document.getElementById("currentParagraph");

let importedDocument = null;
let sourceMessage = "手动输入";
let importInProgress = false;

async function requestAudio({ segment, voice, speed, signal }) {
  const response = await fetch("/v1/audio/speech", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    signal,
    body: JSON.stringify({ voice, input: segment.text, response_format: "wav", speed })
  });
  if (!response.ok) throw new Error((await response.text()) || `HTTP ${response.status}`);
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.startsWith("audio/wav") && !contentType.startsWith("audio/x-wav")) {
    throw new Error(`返回了非 WAV 音频：${contentType}`);
  }
  return response.blob();
}

let queue;
const player = new AudioPlayer(document.getElementById("audio"), {
  onEnded: () => queue.handleEnded(),
  onError: error => queue.handlePlayerError(error)
});
queue = new ReaderQueue({ player, requestAudio, onChange: render });

function render(snapshot = queue.snapshot) {
  const active = ["generating", "playing", "paused"].includes(snapshot.state);
  startButton.disabled = !voiceSelect.value || importInProgress || snapshot.state === "generating" || snapshot.state === "playing";
  startButton.textContent = snapshot.state === "paused" ? "继续" : "开始";
  pauseButton.disabled = snapshot.state !== "playing";
  stopButton.disabled = !active;
  voiceSelect.disabled = active || !voiceSelect.value || importInProgress;
  speedInput.disabled = active || importInProgress;
  textInput.disabled = active || importInProgress;
  fileInput.disabled = active || importInProgress;

  const segment = snapshot.currentSegment;
  chapterBox.textContent = segment ? `当前章节：${segment.chapterTitle}` : "当前章节：—";
  paragraphBox.textContent = segment ? segment.originalText : "当前段落：—";
  const position = segment ? `${snapshot.index + 1}/${snapshot.total}` : "";
  const messages = {
    idle: "已就绪。",
    generating: `正在生成第 ${position} 个片段……`,
    playing: `正在播放第 ${position} 个片段${snapshot.prefetchReady ? "；下一段已就绪" : ""}。`,
    paused: `已暂停在第 ${position} 个片段${snapshot.prefetchReady ? "；下一段已就绪" : ""}。`,
    stopped: snapshot.error ? `朗读失败：${snapshot.error.message}` : "已停止。",
    finished: `朗读完成，共 ${snapshot.total} 个片段。`
  };
  statusBox.textContent = messages[snapshot.state];
  sourceBox.textContent = `文本来源：${sourceMessage}`;
}

async function loadVoices() {
  try {
    const response = await fetch("/v1/voices");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    voiceSelect.innerHTML = "";
    for (const voice of data.voices || []) {
      if (voice.error) continue;
      const option = document.createElement("option");
      option.value = voice.id;
      option.textContent = voice.name || voice.id;
      voiceSelect.appendChild(option);
    }
    if (!voiceSelect.options.length) {
      statusBox.textContent = "没有可用角色，请先在 voices/ 中添加配置。";
      return;
    }
    render();
  } catch (error) {
    statusBox.textContent = `无法读取角色列表：${error}`;
  }
}

async function importTxt() {
  const file = fileInput.files?.[0];
  if (!file) return;
  importInProgress = true;
  render();
  statusBox.textContent = "正在读取 TXT……";
  try {
    const parsed = await readTxtFile(file);
    importedDocument = parsed;
    sourceMessage = `TXT：${file.name}`;
    textInput.value = parsed.chapters.map(chapter =>
      `${chapter.title}\n\n${chapter.paragraphs.join("\n\n")}`
    ).join("\n\n");
    sourceBox.textContent = `文本来源：${sourceMessage}，${parsed.chapters.length} 章`;
    statusBox.textContent = "TXT 已导入，点击开始朗读。";
  } catch (error) {
    statusBox.textContent = `TXT 导入失败：${error.message}`;
  } finally {
    importInProgress = false;
    fileInput.value = "";
    const message = statusBox.textContent;
    render();
    statusBox.textContent = message;
  }
}

async function startOrResume() {
  if (queue.state === "paused") {
    await queue.resume();
    return;
  }
  const speed = Number(speedInput.value);
  if (!Number.isFinite(speed) || speed <= 0) {
    statusBox.textContent = "速度必须大于 0。";
    return;
  }
  try {
    const document = importedDocument || documentFromManual(textInput.value);
    const segments = segmentDocument(document);
    if (!segments.length) throw new Error("请输入或导入有内容的文本。");
    await queue.start(segments, { voice: voiceSelect.value, speed });
  } catch (error) {
    statusBox.textContent = error.message;
  }
}

textInput.addEventListener("input", () => {
  importedDocument = null;
  sourceMessage = "手动输入";
  render();
});
fileInput.addEventListener("change", importTxt);
startButton.addEventListener("click", startOrResume);
pauseButton.addEventListener("click", () => queue.pause());
stopButton.addEventListener("click", () => queue.stop());
loadVoices();
