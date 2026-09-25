import { documentFromManual, readTxtFile } from "./sources.js";
import { readEpubFile } from "./epub_source.js";
import { segmentDocument } from "./segmenter.js";
import { AudioPlayer } from "./player.js";
import { ReaderQueue } from "./queue.js";

const voiceSelect = document.getElementById("voice");
const modelSelect = document.getElementById("modelId");
const referenceSelect = document.getElementById("referenceId");
const speedInput = document.getElementById("speed");
const textInput = document.getElementById("text");
const fileInput = document.getElementById("txtFile");
const epubInput = document.getElementById("epubFile");
const startButton = document.getElementById("start");
const pauseButton = document.getElementById("pause");
const stopButton = document.getElementById("stop");
const statusBox = document.getElementById("status");
const sourceBox = document.getElementById("source");
const bookTitleBox = document.getElementById("bookTitle");
const bookAuthorBox = document.getElementById("bookAuthor");
const chapterBox = document.getElementById("currentChapter");
const paragraphBox = document.getElementById("currentParagraph");

let importedDocument = null;
let sourceMessage = "手动输入";
let bookMetadata = { title: "手动输入", author: "" };
let importInProgress = false;
let voiceCatalog = new Map();

async function requestAudio({ segment, voice, modelId, referenceId, speed, signal }) {
  const response = await fetch("/v1/audio/speech", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    signal,
    body: JSON.stringify({
      voice,
      model_id: modelId || null,
      reference_id: referenceId || null,
      input: segment.text,
      response_format: "wav",
      speed
    })
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
  modelSelect.disabled = active || !modelSelect.options.length || importInProgress;
  referenceSelect.disabled = active || !referenceSelect.options.length || importInProgress;
  speedInput.disabled = active || importInProgress;
  textInput.disabled = active || importInProgress;
  fileInput.disabled = active || importInProgress;
  epubInput.disabled = active || importInProgress;

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
  bookTitleBox.textContent = `书名：${bookMetadata.title}`;
  bookAuthorBox.textContent = `作者：${bookMetadata.author || "未提供"}`;
}

function fillAssetSelect(select, items, defaultId, labelBuilder) {
  select.innerHTML = "";
  for (const item of items || []) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = labelBuilder(item);
    select.appendChild(option);
  }
  if (defaultId && [...select.options].some(option => option.value === defaultId)) {
    select.value = defaultId;
  }
}

function syncCharacterAssets() {
  const voice = voiceCatalog.get(voiceSelect.value);
  if (!voice) {
    modelSelect.innerHTML = "";
    referenceSelect.innerHTML = "";
    return;
  }

  fillAssetSelect(
    modelSelect,
    voice.models,
    voice.default_model,
    item => [item.name || item.id, item.version].filter(Boolean).join(" · ")
  );
  fillAssetSelect(
    referenceSelect,
    voice.references,
    voice.default_reference,
    item => {
      const details = [item.emotion, item.intensity === null || item.intensity === undefined ? "" : item.intensity]
        .filter(value => value !== "");
      return details.length
        ? `${item.name || item.id} · ${details.join(" · ")}`
        : (item.name || item.id);
    }
  );
}

async function loadVoices() {
  try {
    const response = await fetch("/v1/voices");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    voiceSelect.innerHTML = "";
    voiceCatalog = new Map();
    for (const voice of data.voices || []) {
      if (voice.error) continue;
      voiceCatalog.set(voice.id, voice);
      const option = document.createElement("option");
      option.value = voice.id;
      option.textContent = voice.name || voice.id;
      voiceSelect.appendChild(option);
    }
    if (!voiceSelect.options.length) {
      statusBox.textContent = "没有可用角色，请先在 voices/ 中添加配置。";
      return;
    }
    syncCharacterAssets();
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
    bookMetadata = { title: parsed.title, author: "" };
    textInput.value = parsed.chapters.map(chapter =>
      `${chapter.title}\n\n${chapter.paragraphs.join("\n\n")}`
    ).join("\n\n");
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

async function importEpub() {
  const file = epubInput.files?.[0];
  if (!file) return;
  importInProgress = true;
  render();
  statusBox.textContent = "正在解析 EPUB……";
  try {
    const parsed = await readEpubFile(file);
    importedDocument = parsed.document;
    bookMetadata = parsed.metadata;
    sourceMessage = `EPUB：${file.name}，${parsed.document.chapters.length} 章`;
    textInput.value = parsed.document.chapters.map(chapter =>
      `${chapter.title}\n\n${chapter.paragraphs.join("\n\n")}`
    ).join("\n\n");
    statusBox.textContent = "EPUB 已导入，点击开始朗读。";
  } catch (error) {
    statusBox.textContent = `EPUB 导入失败：${error.message}`;
  } finally {
    importInProgress = false;
    epubInput.value = "";
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
    await queue.start(segments, {
      voice: voiceSelect.value,
      modelId: modelSelect.value || null,
      referenceId: referenceSelect.value || null,
      speed
    });
  } catch (error) {
    statusBox.textContent = error.message;
  }
}

voiceSelect.addEventListener("change", () => {
  syncCharacterAssets();
  render();
});

textInput.addEventListener("input", () => {
  importedDocument = null;
  sourceMessage = "手动输入";
  bookMetadata = { title: "手动输入", author: "" };
  render();
});
fileInput.addEventListener("change", importTxt);
epubInput.addEventListener("change", importEpub);
startButton.addEventListener("click", startOrResume);
pauseButton.addEventListener("click", () => queue.pause());
stopButton.addEventListener("click", () => queue.stop());
loadVoices();
