import './index.css'

const stages = ['素材', '分析', '計畫', '剪輯', '匯出']

function App() {
  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="Clipen 作品首頁">clipen</a>
        <span className="local-badge">本機工作台</span>
      </header>
      <main>
        <section className="hero" aria-labelledby="hero-title">
          <p className="eyebrow">你的作品</p>
          <h1 id="hero-title">從素材開始，讓計畫先說清楚。</h1>
          <p className="intro">Clipen 會整理毛片與參考風格，提出一份可以修改的剪輯計畫。只有你確認後，作品才會開始執行。</p>
        </section>
        <section className="workflow" aria-label="Clipen 作品流程">
          {stages.map((stage, index) => (
            <div className="stage" key={stage}>
              <span aria-hidden="true">{index + 1}</span>
              <strong>{stage}</strong>
            </div>
          ))}
        </section>
        <section className="empty-state" aria-labelledby="empty-title">
          <div>
            <p className="eyebrow">作品庫</p>
            <h2 id="empty-title">尚未建立作品</h2>
            <p>建立作品後，這裡會顯示目前階段、最近更新與唯一的下一步。</p>
          </div>
        </section>
      </main>
    </div>
  )
}

export default App
