const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export default function HomePage() {
  return (
    <main className="shell">
      <h1>MetaSearch</h1>
      <p className="tagline">
        Meta-search engine cho AI: gom kết quả từ nhiều engine, trích xuất nội dung sạch, trả lời có
        trích dẫn.
      </p>

      <section className="card">
        <h2>Trạng thái scaffold</h2>
        <p>
          Phase 0 đang hoàn tất phần khung. UI tìm kiếm đầy đủ sẽ được bổ sung ở Phase 2 theo{" "}
          <code>docs/12-roadmap-plan.md</code>.
        </p>
        <ul>
          <li>
            API base URL: <code>{API_BASE_URL}</code>
          </li>
          <li>
            Health check: <code>{API_BASE_URL}/healthz</code>
          </li>
          <li>
            API docs: <code>{API_BASE_URL}/docs</code>
          </li>
        </ul>
      </section>
    </main>
  );
}
