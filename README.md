# Clipen

開發中的桌面 AI 剪輯工具。本提交先建立公開產品架構與已簽署系統 Skills，
尚未提供剪輯功能。後續本機 UI 與文字通道會以獨立提交加入。

- [產品架構](ARCHITECTURE.md)
- [系統 Skills 保護與限制](docs/SKILL_PROTECTION.md)

```sh
python3 -m tools.skill_guard verify
python3 -m unittest discover -s tests -p 'test_skill_guard.py' -v
```

開發文件、個人設定、憑證與素材不隨本產品發布。
