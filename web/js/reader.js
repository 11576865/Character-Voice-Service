import { documentFromManual, readTxtFile } from "./sources.js";
import { readEpubFile } from "./epub_source.js";
import { segmentDocument } from "./segmenter.js";
import { AudioPlayer } from "./player.js";
import { ReaderQueue } from "./queue.js";
import { ProgressStore, documentIdForFile } from "./progress.js";
import { ReaderNavigation, chapterStart, chapterPosition } from "./navigation.js";

const element = id => document.getElementById(id);
const ui = {
  voice: element("voice"), modelId: element("modelId"), referenceId: element("referenceId"),
  speed: element("speed"), text: element("text"),
  txtFile: element("txtFile"), epubFile: element("epubFile"),
  manualPanel: element("manualPanel"),
  useManual: element("useManual"), start: element("start"), pause: element("pause"), stop: element("stop"),
  previousSegment: element("previousSegment"), nextSegment: element("nextSegment"),
  previousChapter: element("previousChapter"), nextChapter: element("nextChapter"),
  resumePrompt: element("resumePrompt"), resumeText: element("resumeText"),
  resumeLast: element("resumeLast"), restartBook: element("restartBook"),
  source: element("source"), bookTitle: element("bookTitle"), bookAuthor: element("bookAuthor"),
  chapterCount: element("chapterCount"), chapterList: element("chapterList"),
  chaptersPanel: element("chaptersPanel"), readingPane: element("readingPane"),
  documentBody: element("documentBody"), currentChapter: element("currentChapter"),
  position: element("position"), status: element("status"), storageNotice: element("storageNotice")
};

const progressStore = new ProgressStore();
let currentDocument = null;
let documentId = null;
let bookAuthor = "";
let sourceLabel = "未加载";
let segments = [];
let navigation = null;
let pendingProgress = null;
let stoppedPosition = null;
let loading = false;
let importSerial = 0;
let statusOverride = null;
let lastSaveAt = 0;
let highlightedIndex = -1;
let forceScrollIndex = -1;
let userScrollUntil = 0;
let jumpSavedIndex = -1;
let voiceCatalog = new Map();
const segmentNodes = new Map();
const chapterButtons = [];

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
const player = new AudioPlayer(element("audio"), {
  onEnded: () => queue.handleEnded(),
  onError: error => queue.handlePlayerError(error),
  onTimeUpdate: time => {
    if (queue?.state === "playing") saveProgress(false, time);
  }
});
queue = new ReaderQueue({ player, requestAudio, onChange: onQueueChange });

function playbackOptions() {
  const speed = Number(ui.speed.value);
  if (!ui.voice.value) throw new Error("请选择角色。");
  if (!Number.isFinite(speed) || speed <= 0) throw new Error("速度必须大于 0。");
  return {
    voice: ui.voice.value,
    modelId: ui.modelId.value || null,
    referenceId: ui.referenceId.value || null,
    speed
  };
}

function activePosition() {
  if (!navigation) return null;
  const snapshot = queue.snapshot;
  const index = snapshot.currentSegment ? snapshot.index : navigation.lastIndex;
  return segments[index] ? { index, segment: segments[index] } : null;
}

function saveProgress(force = false, audioTime = player.currentTime) {
  if (!documentId || !currentDocument || !navigation) return;
  if (!force && Date.now() - lastSaveAt < 2000) return;
  const position = activePosition();
  if (!position) return;
  const time = queue.state === "playing" || queue.state === "paused" ? audioTime : 0;
  progressStore.save({
    documentId,
    title: currentDocument.title,
    chapterIndex: position.segment.chapterIndex,
    segmentIndex: position.index,
    audioTime: time
  });
  lastSaveAt = Date.now();
  ui.storageNotice.hidden = progressStore.available;
}

function onQueueChange(snapshot) {
  navigation?.sync(snapshot);
  if (snapshot.currentSegment && ["generating", "playing", "paused"].includes(snapshot.state)) {
    const changed = highlightedIndex !== snapshot.index;
    if (snapshot.index === jumpSavedIndex) {
      // onJump already wrote the target position and seek time.
      if (snapshot.state === "playing") jumpSavedIndex = -1;
    } else if (changed) {
      saveProgress(true, 0);
    } else if (snapshot.state === "paused") {
      saveProgress(true, player.currentTime);
    }
  }
  statusOverride = null;
  render(snapshot);
}

function scrollToSegment(node, force = false) {
  if (!force && Date.now() < userScrollUntil) return;
  const pane = ui.readingPane;
  const paneRect = pane.getBoundingClientRect();
  const rect = node.getBoundingClientRect();
  const margin = 64;
  if (rect.top >= paneRect.top + margin && rect.bottom <= paneRect.bottom - margin) return;
  const offset = rect.top - paneRect.top;
  const target = pane.scrollTop + offset - Math.max(60, pane.clientHeight * 0.25);
  pane.scrollTo({ top: Math.max(0, target), behavior: force ? "auto" : "smooth" });
}

function highlightSegment(index) {
  if (index === highlightedIndex && forceScrollIndex !== index) return;
  segmentNodes.get(highlightedIndex)?.classList.remove("active");
  const node = segmentNodes.get(index);
  highlightedIndex = index;
  if (!node) return;
  node.classList.add("active");
  scrollToSegment(node, forceScrollIndex === index);
  forceScrollIndex = -1;
}

function renderBody() {
  ui.documentBody.replaceChildren();
  ui.chapterList.replaceChildren();
  segmentNodes.clear();
  chapterButtons.length = 0;
  highlightedIndex = -1;
  if (!currentDocument) {
    ui.documentBody.textContent = "请选择 TXT / EPUB，或使用粘贴文本。";
    return;
  }
  const paragraphSegments = new Map();
  for (const segment of segments) {
    const key = `${segment.chapterIndex}:${segment.paragraphIndex}`;
    if (!paragraphSegments.has(key)) paragraphSegments.set(key, []);
    paragraphSegments.get(key).push(segment);
  }
  currentDocument.chapters.forEach((chapter, chapterIndex) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${chapterIndex + 1}. ${chapter.title}`;
    button.disabled = chapterStart(segments, chapterIndex) < 0;
    button.addEventListener("click", () => {
      jump(() => navigation.jumpToChapter(chapterIndex));
      if (window.matchMedia("(max-width: 700px)").matches) ui.chaptersPanel.open = false;
    });
    ui.chapterList.appendChild(button);
    chapterButtons.push(button);

    const section = document.createElement("section");
    const heading = document.createElement("h2");
    heading.textContent = chapter.title;
    section.appendChild(heading);
    chapter.paragraphs.forEach((paragraph, paragraphIndex) => {
      const p = document.createElement("p");
      const pieces = paragraphSegments.get(`${chapterIndex}:${paragraphIndex}`) || [];
      if (!pieces.length) p.textContent = paragraph;
      for (const segment of pieces) {
        const span = document.createElement("span");
        span.className = "segment";
        span.textContent = segment.text;
        p.appendChild(span);
        segmentNodes.set(segment.index, span);
      }
      section.appendChild(p);
    });
    ui.documentBody.appendChild(section);
  });
}

function render(snapshot = queue.snapshot) {
  const active = ["generating", "playing", "paused"].includes(snapshot.state);
  const hasDocument = Boolean(currentDocument && segments.length);
  const position = activePosition();
  const index = position?.index ?? 0;
  const chapterIndex = position?.segment.chapterIndex ?? 0;
  const localPosition = chapterPosition(segments, index);
  const awaitingChoice = Boolean(pendingProgress);

  ui.start.disabled = !hasDocument || !ui.voice.value || loading || active && snapshot.state !== "paused" || awaitingChoice;
  ui.start.textContent = snapshot.state === "paused" || stoppedPosition ? "继续" : "开始";
  ui.pause.disabled = snapshot.state !== "playing";
  ui.stop.disabled = !active;
  ui.voice.disabled = active || loading || !ui.voice.value;
  ui.modelId.disabled = active || loading || !ui.modelId.options.length;
  ui.referenceId.disabled = active || loading || !ui.referenceId.options.length;
  ui.speed.disabled = active || loading;
  ui.useManual.disabled = loading;
  ui.previousSegment.disabled = !hasDocument || loading || awaitingChoice || index <= 0;
  ui.nextSegment.disabled = !hasDocument || loading || awaitingChoice || index >= segments.length - 1;
  ui.previousChapter.disabled = !hasDocument || loading || awaitingChoice ||
    !segments.some(segment => segment.chapterIndex < chapterIndex);
  ui.nextChapter.disabled = !hasDocument || loading || awaitingChoice ||
    !segments.some(segment => segment.chapterIndex > chapterIndex);
  ui.resumePrompt.hidden = !awaitingChoice;
  ui.source.textContent = `文本来源：${sourceLabel}`;
  ui.bookTitle.textContent = `书名：${currentDocument?.title || "—"}`;
  ui.bookAuthor.textContent = `作者：${bookAuthor || "未提供"}`;
  ui.chapterCount.textContent = `章节：${currentDocument?.chapters.length ?? "—"}`;
  ui.storageNotice.hidden = progressStore.available;

  if (hasDocument) {
    ui.currentChapter.textContent = `当前章节：${currentDocument.chapters[chapterIndex]?.title || "—"}`;
    ui.position.textContent = `第 ${chapterIndex + 1}/${currentDocument.chapters.length} 章 · 本章片段 ${localPosition.index}/${localPosition.total} · 全书片段 ${index + 1}/${segments.length}`;
    chapterButtons.forEach((button, i) => button.classList.toggle("active", i === chapterIndex));
    highlightSegment(index);
  } else {
    ui.currentChapter.textContent = "当前章节：—";
    ui.position.textContent = "尚未开始阅读";
  }

  const number = snapshot.currentSegment ? `${snapshot.index + 1}/${snapshot.total}` : "";
  const messages = {
    idle: hasDocument ? "已就绪。" : "请选择或粘贴文本。",
    generating: `正在生成第 ${number} 个片段……`,
    playing: `正在播放第 ${number} 个片段${snapshot.prefetchReady ? "；下一段已就绪" : ""}。`,
    paused: `已暂停在第 ${number} 个片段。`,
    stopped: snapshot.error ? `朗读失败：${snapshot.error.message}` : "已停止，当前位置已保留。",
    finished: `朗读完成，共 ${snapshot.total} 个片段。`
  };
  ui.status.textContent = statusOverride || (loading ? "正在读取文件……" : messages[snapshot.state]);
}

function showDocument(model, metadata, id, label) {
  currentDocument = model;
  documentId = id;
  bookAuthor = metadata.author || "";
  sourceLabel = label;
  segments = segmentDocument(model);
  pendingProgress = id ? progressStore.load(id, model, segments) : null;
  stoppedPosition = null;
  jumpSavedIndex = -1;
  navigation = new ReaderNavigation({
    document: model,
    segments,
    queue,
    getPlaybackOptions: playbackOptions,
    onJump: ({ index, audioTime }) => {
      stoppedPosition = null;
      forceScrollIndex = index;
      jumpSavedIndex = index;
      if (id) progressStore.save({
        documentId: id, title: model.title,
        chapterIndex: segments[index].chapterIndex, segmentIndex: index, audioTime
      });
    }
  });
  if (pendingProgress) {
    navigation.lastIndex = pendingProgress.segmentIndex;
    forceScrollIndex = pendingProgress.segmentIndex;
    ui.resumeText.textContent = `检测到上次阅读位置：第 ${pendingProgress.chapterIndex + 1} 章，全书第 ${pendingProgress.segmentIndex + 1} 个片段。`;
  }
  renderBody();
  statusOverride = segments.length ? "文档已加载。" : "文档没有可朗读的正文。";
  render();
}

function stopForSourceChange() {
  saveProgress(true);
  queue.stop();
  pendingProgress = null;
  stoppedPosition = null;
}

async function importFile(file, kind) {
  if (!file) return;
  const serial = ++importSerial;
  stopForSourceChange();
  loading = true;
  statusOverride = null;
  render();
  try {
    const buffer = await file.arrayBuffer();
    const id = documentIdForFile(buffer, kind);
    const parsed = kind === "txt"
      ? { document: await readTxtFile(file, buffer), metadata: { author: "" } }
      : await readEpubFile(file, buffer);
    if (serial !== importSerial) return;
    showDocument(parsed.document, parsed.metadata, id, `${kind.toUpperCase()}：${file.name}`);
  } catch (error) {
    if (serial === importSerial) statusOverride = `文件导入失败：${error.message}`;
  } finally {
    if (serial === importSerial) {
      loading = false;
      ui.txtFile.value = "";
      ui.epubFile.value = "";
      render();
    }
  }
}

function useManualText() {
  ++importSerial;
  stopForSourceChange();
  showDocument(documentFromManual(ui.text.value), { author: "" }, null, "手动输入");
  ui.manualPanel?.removeAttribute("open");
}

async function jump(action) {
  if (!navigation || pendingProgress) return;
  try {
    statusOverride = null;
    const moved = await action();
    if (!moved) render();
  } catch (error) {
    statusOverride = error.message;
    render();
  }
}

async function startOrResume() {
  if (!navigation || pendingProgress) return;
  if (queue.state === "paused") {
    await queue.resume();
    return;
  }
  const position = stoppedPosition;
  stoppedPosition = null;
  await jump(() => navigation.jumpToSegment(position?.index ?? 0, position?.audioTime ?? 0));
}

function stopReading() {
  if (!navigation) return;
  const position = activePosition();
  if (position) {
    stoppedPosition = { index: position.index, audioTime: player.currentTime };
    saveProgress(true, stoppedPosition.audioTime);
  }
  queue.stop();
}

function chooseProgress(continueReading) {
  if (!pendingProgress || !navigation) return;
  const position = continueReading
    ? { index: pendingProgress.segmentIndex, audioTime: pendingProgress.audioTime }
    : { index: 0, audioTime: 0 };
  pendingProgress = null;
  render();
  jump(() => navigation.jumpToSegment(position.index, position.audioTime));
}

function fillAssetSelect(select, items, defaultId, labelBuilder) {
  select.replaceChildren();
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
  const voice = voiceCatalog.get(ui.voice.value);
  if (!voice) {
    ui.modelId.replaceChildren();
    ui.referenceId.replaceChildren();
    return;
  }
  fillAssetSelect(
    ui.modelId,
    voice.models,
    voice.default_model,
    item => [item.name || item.id, item.version].filter(Boolean).join(" · ")
  );
  fillAssetSelect(
    ui.referenceId,
    voice.references,
    voice.default_reference,
    item => {
      const details = [
        item.emotion,
        item.intensity === null || item.intensity === undefined ? "" : item.intensity
      ].filter(value => value !== "");
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
    ui.voice.replaceChildren();
    voiceCatalog = new Map();
    for (const voice of data.voices || []) {
      if (voice.error) continue;
      voiceCatalog.set(voice.id, voice);
      const option = document.createElement("option");
      option.value = voice.id;
      option.textContent = voice.name || voice.id;
      ui.voice.appendChild(option);
    }
    if (!ui.voice.options.length) {
      syncCharacterAssets();
      statusOverride = "没有可用角色，请先在 voices/ 中添加配置。";
    } else {
      syncCharacterAssets();
    }
    render();
  } catch (error) {
    statusOverride = `无法读取角色列表：${error}`;
    render();
  }
}

ui.voice.addEventListener("change", () => {
  syncCharacterAssets();
  render();
});
ui.readingPane.addEventListener("wheel", () => { userScrollUntil = Date.now() + 8000; }, { passive: true });
ui.readingPane.addEventListener("touchstart", () => { userScrollUntil = Date.now() + 8000; }, { passive: true });
ui.readingPane.addEventListener("pointerdown", () => { userScrollUntil = Date.now() + 8000; });
ui.txtFile.addEventListener("change", () => importFile(ui.txtFile.files?.[0], "txt"));
ui.epubFile.addEventListener("change", () => importFile(ui.epubFile.files?.[0], "epub"));
ui.useManual.addEventListener("click", useManualText);
ui.start.addEventListener("click", startOrResume);
ui.pause.addEventListener("click", () => queue.pause());
ui.stop.addEventListener("click", stopReading);
ui.previousSegment.addEventListener("click", () => jump(() => navigation.previousSegment()));
ui.nextSegment.addEventListener("click", () => jump(() => navigation.nextSegment()));
ui.previousChapter.addEventListener("click", () => jump(() => navigation.previousChapter()));
ui.nextChapter.addEventListener("click", () => jump(() => navigation.nextChapter()));
ui.resumeLast.addEventListener("click", () => chooseProgress(true));
ui.restartBook.addEventListener("click", () => chooseProgress(false));
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") saveProgress(true);
});
window.addEventListener("pagehide", () => saveProgress(true));
render();
loadVoices();
