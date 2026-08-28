'use strict';
globalThis.ClipenPreparation = Object.freeze({
  fields: Object.freeze({source_directory:'source-directory', reference_directory:'reference-directory', brief:'brief'}),
  same(a, b) {
    return !!a && !!b && ['source_directory','reference_directory','brief'].every((name) => a[name] === b[name]);
  },
  invalid(values) {
    for (const name of ['source_directory','reference_directory','brief']) {
      const text = values[name];
      const controls = name === 'brief' ? /[\u0000-\u0008\u000b-\u001f\u007f]/ : /[\u0000-\u001f\u007f]/;
      if (typeof text !== 'string' || controls.test(text) ||
          new TextEncoder().encode(text).length > (name === 'brief' ? 8000 : 4096)) return name;
    }
    return null;
  },
  display(draft) {
    return '毛片目錄：' + draft.source_directory + '\n參考目錄：' + draft.reference_directory + '\n\n剪輯需求：\n' + draft.brief;
  },
  pollDelay(hidden) { return hidden ? 15000 : 2000; },
});
