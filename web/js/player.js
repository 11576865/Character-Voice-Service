export class AudioPlayer {
  constructor(audioElement, { onEnded, onError } = {}) {
    this.audio = audioElement;
    this.objectUrl = null;
    audioElement.addEventListener("ended", () => onEnded?.());
    audioElement.addEventListener("error", () => {
      if (this.objectUrl) onError?.(new Error("浏览器无法播放 WAV 音频。"));
    });
  }

  async play(blob) {
    this.stop();
    this.objectUrl = URL.createObjectURL(blob);
    this.audio.src = this.objectUrl;
    await this.audio.play();
  }

  pause() {
    this.audio.pause();
  }

  async resume() {
    await this.audio.play();
  }

  stop() {
    const oldUrl = this.objectUrl;
    this.objectUrl = null;
    this.audio.pause();
    this.audio.removeAttribute("src");
    this.audio.load();
    if (oldUrl) URL.revokeObjectURL(oldUrl);
  }
}
