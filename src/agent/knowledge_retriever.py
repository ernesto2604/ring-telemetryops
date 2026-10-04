import json
from pathlib import Path
from typing import List, Dict, Any


class KnowledgeRetriever:
    def __init__(self, kb_path: str = "data/knowledge_base/ring_video_troubleshooting.json"):
        self.kb_path = Path(kb_path)
        if not self.kb_path.exists():
            raise FileNotFoundError(f"Base de conocimiento no encontrada en: {self.kb_path}")
        self.kb_data = self._load_kb()

    def _load_kb(self) -> List[Dict[str, Any]]:
        with open(self.kb_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def retrieve_guidance(self, diagnostic_report: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Recupera los artículos de la KB relevantes según los problemas del reporte."""
        if diagnostic_report.get("status") == "OPTIMAL":
            return []

        matched_articles = []
        issues_text = " ".join(diagnostic_report.get("issues", [])).lower()
        fps = diagnostic_report.get("fps", 30.0)
        bitrate = diagnostic_report.get("bitrate_kbps", 500.0)

        for article in self.kb_data:
            category = article.get("issue_category", "")
            
            # Criterio para bitrate bajo y resolución degradada
            if category == "low_bitrate_and_pixelation":
                if bitrate < 150.0 or "resolución baja" in issues_text or "bitrate" in issues_text:
                    matched_articles.append(article)
                    continue

            # Criterio para caídas de cuadros por segundo / tartamudeo
            if category == "low_frame_rate_stutter":
                if fps < 20.0 or "cuadros" in issues_text or "fps" in issues_text:
                    matched_articles.append(article)
                    continue

        return matched_articles


if __name__ == "__main__":
    from src.video_pipeline.analyzer import VideoAnalyzer

    analyzer = VideoAnalyzer("data/samples/test_stream_bad.mp4")
    report = analyzer.inspect_stream()

    retriever = KnowledgeRetriever()
    guidance = retriever.retrieve_guidance(report)

    print(f"Reporte detectado: {report['status']}")
    print(f"Artículos de troubleshooting recuperados: {len(guidance)}")
    for g in guidance:
        print(f" - [{g['id']}] {g['issue_category']}: {g['root_cause']}")