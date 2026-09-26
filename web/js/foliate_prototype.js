import "/foliate-assets/view.js";
import { readEpubFile } from "./epub_source.js";

const view = document.getElementById("book");
const status = document.getElementById("status");
const differences = document.getElementById("differences");
const toc = document.getElementById("toc");
const paragraphs = document.getElementById("paragraphs");
const locationLabel = document.getElementById("location");
let parsed = null;
let highlighted = null;
let rendered = false;
const sectionMap = new Map();

const clean = text => text.replace(/\s+/g, " ").trim();

function textOf(element) {
  const copy = element.cloneNode(true);
  copy.querySelectorAll("script, style, svg, math, rt, rp").forEach(node => node.remove());
  copy.querySelectorAll("br").forEach(node => node.replaceWith(" "));
  return clean(copy.textContent);
}

function elementsIn(doc) {
  const body = doc.querySelector("body");
  if (!body) return [];
  const nodes = [...body.querySelectorAll("p, li, h1, h2, h3, h4, blockquote, pre")]
    .filter(node => !(node.matches("li, blockquote") && node.querySelector("p, li")));
  if (!nodes.length && textOf(body)) nodes.push(body);
  return nodes.filter(node => textOf(node));
}

function chapterFor(index) {
  const section = view.book?.sections[index];
  if (!section || !parsed) return -1;
  const sectionPath = decodeURIComponent(String(section.id).split(/[?#]/, 1)[0]);
  return parsed.metadata.chapterSources.findIndex(source =>
    sectionPath === source.path || sectionPath.endsWith("/" + source.path));
}

function clearHighlight() {
  highlighted?.classList.remove("cvs-highlight");
  highlighted = null;
}

async function focusParagraph(index, paragraphIndex) {
  const mapping = sectionMap.get(index);
  const element = mapping?.elements[paragraphIndex];
  if (!element || !mapping.matched[paragraphIndex]) return;
  const range = element.ownerDocument.createRange();
  range.selectNodeContents(element);
  const cfi = view.getCFI(index, range);
  if (!await view.goTo(cfi)) return;
  clearHighlight();
  element.classList.add("cvs-highlight");
  highlighted = element;
  locationLabel.textContent = "已定位 CVS 第 " + (paragraphIndex + 1) + " 段；CFI " + cfi;
}

function renderParagraphs(index) {
  paragraphs.replaceChildren();
  const mapping = sectionMap.get(index);
  if (!mapping || mapping.chapterIndex < 0) return;
  parsed.document.chapters[mapping.chapterIndex].paragraphs.forEach((text, paragraphIndex) => {
    const button = document.createElement("button");
    button.type = "button";
    button.disabled = !mapping.matched[paragraphIndex];
    button.textContent = (paragraphIndex + 1) + ". " + text.slice(0, 72);
    button.title = text;
    button.addEventListener("click", () => focusParagraph(index, paragraphIndex));
    paragraphs.append(button);
  });
}

function flattenToc(items, depth = 0) {
  for (const item of items || []) {
    if (item.href) {
      const option = document.createElement("option");
      option.value = item.href;
      option.textContent = "　".repeat(depth) + (item.label || item.href);
      toc.append(option);
    }
    flattenToc(item.subitems, depth + 1);
  }
}

function canRenderBlobFrame() {
  const marker = "cvs-epub-frame-check";
  const url = URL.createObjectURL(new Blob([
    "<!doctype html><html><body>" + marker + "</body></html>"
  ], { type: "text/html" }));
  const frame = document.createElement("iframe");
  frame.setAttribute("sandbox", "allow-same-origin allow-scripts");
  frame.style.cssText = "position:absolute;width:1px;height:1px;left:-10000px;top:0";
  document.body.append(frame);
  return new Promise(resolve => {
    let finished = false;
    const finish = result => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      frame.remove();
      URL.revokeObjectURL(url);
      resolve(result);
    };
    const timer = setTimeout(() => finish(false), 4000);
    frame.onload = () => {
      try {
        if (frame.contentDocument?.body?.textContent?.includes(marker)) finish(true);
      } catch (_) { finish(false); }
    };
    frame.onerror = () => finish(false);
    frame.src = url;
  });
}

view.addEventListener("load", event => {
  if (!parsed) return;
  try {
  const { doc, index } = event.detail;
  rendered = true;
  const chapterIndex = chapterFor(index);
  const elements = elementsIn(doc);
  const expected = parsed.document.chapters[chapterIndex]?.paragraphs || [];
  const matched = expected.map((text, i) => !!elements[i] && textOf(elements[i]) === clean(text));
  sectionMap.set(index, { chapterIndex, elements, matched });
  const style = doc.createElement("style");
  style.textContent = ".cvs-highlight { background: #9ef2c4 !important; color: #10281c !important; outline: 2px solid #169b66 !important; }";
  doc.head?.append(style);
  const count = matched.filter(Boolean).length;
  status.textContent = chapterIndex < 0
    ? "EPUB 内容区 " + (index + 1) + " 不在 CVS 可朗读章节内。"
    : "CVS 第 " + (chapterIndex + 1) + " 章「" + parsed.document.chapters[chapterIndex].title + "」：" + count + "/" + expected.length + " 段按顺序精确匹配。";
  differences.textContent = matched.every(Boolean) && elements.length === expected.length ? "段落映射通过。" :
    [...expected.flatMap((text, i) => matched[i] ? [] : ["第 " + (i + 1) + " 段未匹配：" + text.slice(0, 100)]),
      ...(elements.length !== expected.length ? ["foliate-js 显示 " + elements.length + " 段，CVS 有 " + expected.length + " 段。"] : [])]
      .slice(0, 6).join("\n");
  renderParagraphs(index);
  } catch (error) {
    status.textContent = "段落映射失败：" + error.message;
  }
});

view.addEventListener("relocate", event => {
  const { tocItem, cfi } = event.detail;
  locationLabel.textContent = "当前位置：" + (tocItem?.label || "正文") + "；CFI " + (cfi || "未知");
});

document.getElementById("file").addEventListener("change", async event => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    status.textContent = "正在解析和排版 EPUB…";
    parsed = await readEpubFile(file, await file.arrayBuffer());
    sectionMap.clear();
    clearHighlight();
    rendered = false;
    view.close();
    await view.open(file);
    toc.replaceChildren(new Option("选择目录跳转…", ""));
    flattenToc(view.book.toc);
    const mapped = view.book.sections.filter((_, index) => chapterFor(index) >= 0).length;
    status.textContent = "EPUB 章节路径映射：" + mapped + "/" + parsed.document.chapters.length +
      " 章；正在检查排版内容区…";
    if (!await canRenderBlobFrame()) {
      status.textContent = "EPUB 章节路径映射：" + mapped + "/" + parsed.document.chapters.length +
        " 章；当前浏览器无法载入 EPUB 排版所需的 blob 内嵌框架。";
      differences.textContent = "本页尚不能验证目录定位和段落高亮。可在普通 Chrome 或 Edge 中打开本页再试；CVS 正式 Reader 仍可继续使用。";
      return;
    }
    const navigation = view.goTo(0);
    const outcome = await Promise.race([
      navigation.then(() => "ready"),
      new Promise(resolve => setTimeout(() => resolve("timeout"), 12000))
    ]);
    if (outcome === "timeout" && !rendered) {
      status.textContent = "EPUB 章节路径映射：" + mapped + "/" + parsed.document.chapters.length +
        " 章通过；排版内容区未在 12 秒内载入，目录和高亮尚未通过验证。";
    }
  } catch (error) { status.textContent = "无法验证：" + error.message; }
});
toc.addEventListener("change", () => { if (toc.value) view.goTo(toc.value); });
document.getElementById("previous").addEventListener("click", () => view.prev());
document.getElementById("next").addEventListener("click", () => view.next());
