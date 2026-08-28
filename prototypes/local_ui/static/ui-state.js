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
  connectionNote(connection) {
    if (!connection?.transport_connected) return '請回到專案目錄開啟 AI，並核對 terminal 的連線要求。';
    if (connection?.session_check?.state === 'checking') return '正在核對 AI 通道；這個步驟不會送出你的文字。';
    if (connection?.session_check?.state === 'unavailable') return 'AI 通道核對未完成，未啟用自動接件。請回到 terminal 確認連線。';
    if (connection.waiting_for_work) return '取件工具已在等待；送出後仍需 AI 提交實際回覆。';
    if (connection?.session_check?.state === 'route_verified') return '已核對通道，但仍需確認原 AI 對話與 terminal；自動接件尚未啟用。';
    return '若 terminal 顯示權限要求，請在該處核對。沒有等待接件時，本頁不會另開模型。';
  },
  pollDelay(hidden) { return hidden ? 15000 : 1000; },
  jobStamp(job) { return `${job.id}:${job.version}`; },
});
