'use strict';
// Pure UI rules shared by the browser and dependency-free regression tests.
globalThis.ClipenUI = Object.freeze({
  input(text) {
    const count = Array.from(text).length;
    const bytes = new TextEncoder().encode(text).length;
    return {count, bytes, valid: text.trim().length > 0 && count <= 1000 && bytes <= 4000};
  },
  ready(connection) {
    return connection?.transport_connected === true && connection?.waiting_for_work === true;
  },
  connectionLabel(connection) {
    if (!connection?.transport_connected) return '尚未連接 AI';
    return connection.waiting_for_work ? 'AI 工具正在等待工作' : '已連線，尚未等待工作';
  },
  pollDelay(hidden) { return hidden ? 15000 : 1000; },
  jobStamp(job) { return `${job.id}:${job.version}`; },
});
