import os
import json
import base64
import argparse
from typing import Dict, Any, Optional
from dotenv import load_dotenv

from src.video_pipeline.analyzer import VideoAnalyzer
from src.agent.knowledge_retriever import KnowledgeRetriever

load_dotenv()


class SupportConcierge:
    """
    Orquestador principal del agente de soporte técnico para Ring.
    Conecta el análisis cuantitativo de vídeo, la base de conocimiento y el LLM.
    """

    def __init__(
        self,
        kb_path: str = "data/knowledge_base/ring_video_troubleshooting.json",
        aws_region: str = "us-east-1",
        model_id: str = "anthropic.claude-3-haiku-20240307-v1:0"
    ):
        self.retriever = KnowledgeRetriever(kb_path=kb_path)
        self.aws_region = os.getenv("AWS_DEFAULT_REGION", aws_region)
        self.model_id = model_id
        self._bedrock_client = None

    def _get_bedrock_client(self):
        """Inicializa el cliente de Bedrock bajo demanda con control de excepciones."""
        if self._bedrock_client is None:
            try:
                import boto3
                self._bedrock_client = boto3.client(
                    service_name="bedrock-runtime",
                    region_name=self.aws_region
                )
            except Exception as e:
                print(f"[WARN] No se pudo inicializar AWS Bedrock: {e}")
                self._bedrock_client = None
        return self._bedrock_client

    def _encode_image_to_base64(self, image_path: str) -> Optional[str]:
        """Lee un archivo JPEG en disco y lo convierte a Base64 para el LLM."""
        if not os.path.exists(image_path):
            return None
        with open(image_path, "rb") as img_file:
            return base64.b64encode(img_file.read()).decode("utf-8")

    def _generate_mock_report(self, diagnosis: Dict[str, Any], articles: list) -> str:
        """Fallback local cuando no hay credenciales activas de AWS."""
        issues = diagnosis.get("issues", [])
        
        report = []
        report.append("==================================================")
        report.append("  INFORME DE DIAGNÓSTICO TÉCNICO RING (MODO LOCAL)")
        report.append("==================================================")
        report.append(f"Estado de la transmisión: {diagnosis.get('status')}")
        report.append(f"Resolución: {diagnosis.get('resolution')}")
        report.append(f"FPS: {diagnosis.get('fps')} | Bitrate: {diagnosis.get('bitrate_kbps')} kbps")
        report.append(f"Nitidez (Laplaciano): {diagnosis.get('sharpness_score')}")
        report.append("\nProblemas identificados:")
        for issue in issues:
            report.append(f" - {issue}")

        report.append("\nGuías oficiales aplicadas (RAG):")
        for art in articles:
            report.append(f"\n[{art.get('id')}] - {art.get('issue_category')}")
            report.append(f"Causa raíz: {art.get('root_cause')}")
            report.append("Acciones recomendadas:")
            for action in art.get("recommended_actions", []):
                report.append(f"  * {action}")
        report.append("==================================================")
        return "\n".join(report)

    def _call_bedrock(self, prompt: str, image_base64: Optional[str]) -> str:
        """Invoca a Claude mediante Amazon Bedrock Runtime."""
        client = self._get_bedrock_client()
        if client is None:
            raise RuntimeError("Cliente Bedrock no disponible")

        content = []
        if image_base64:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": image_base64
                }
            })
        
        content.append({
            "type": "text",
            "text": prompt
        })

        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "user",
                    "content": content
                }
            ]
        })

        response = client.invoke_model(
            modelId=self.model_id,
            contentType="application/json",
            accept="application/json",
            body=body
        )

        response_body = json.loads(response["body"].read().decode("utf-8"))
        return response_body["content"][0]["text"]

    def process_stream(self, video_path: str) -> Dict[str, Any]:
        """
        Ejecuta el pipeline completo:
        1. Análisis cuantitativo de vídeo.
        2. Recuperación de conocimiento (RAG).
        3. Síntesis multimodal con LLM.
        """
        print(f"\n[1/3] Analizando stream: {video_path}...")
        analyzer = VideoAnalyzer(video_path)
        diagnosis = analyzer.inspect_stream()
        
        status = diagnosis.get("status")
        if status == "OPTIMAL":
            optimal_report = [
                "==================================================",
                "  INFORME DE TELEMETRÍA RING: ESTADO ÓPTIMO",
                "==================================================",
                f"Estado: {status}",
                f"Resolución: {diagnosis.get('resolution')}",
                f"FPS: {diagnosis.get('fps')} | Bitrate: {diagnosis.get('bitrate_kbps')} kbps",
                f"Nitidez (Laplaciano): {diagnosis.get('sharpness_score')}",
                "\nDiagnóstico:",
                "  El análisis técnico confirma que el flujo de vídeo cumple con todos",
                "  los estándares de rendimiento requeridos (1080p, 30 FPS y tasa de",
                "  transferencia estable). No se requieren acciones correctivas.",
                "=================================================="
            ]
            return {
                "status": "SUCCESS",
                "diagnosis": diagnosis,
                "resolution_report": "\n".join(optimal_report)
            }

        print("[2/3] Stream degradado detectado. Recuperando guías técnicas del manual...")
        articles = self.retriever.retrieve_guidance(diagnosis)

        evidence_frame = diagnosis.get("evidence_frame")
        image_base64 = None
        if evidence_frame:
            image_base64 = self._encode_image_to_base64(evidence_frame)

        print("[3/3] Generando informe técnico...")
        
        prompt = (
            "Actúa como un ingeniero senior de soporte técnico de Amazon Ring.\n"
            "Se ha detectado una degradación en la transmisión de vídeo de un dispositivo.\n\n"
            f"TELEMETRÍA EXTRAÍDA:\n{json.dumps(diagnosis, indent=2, ensure_ascii=False)}\n\n"
            f"PROCEDIMIENTOS OFICIALES DE SOPORTE:\n{json.dumps(articles, indent=2, ensure_ascii=False)}\n\n"
            "INSTRUCCIONES:\n"
            "1. Analiza el fotograma adjunto (si está presente) y describe las anomalías visuales observadas.\n"
            "2. Correlaciona la evidencia visual con las métricas numéricas registradas.\n"
            "3. Proporciona un diagnóstico claro y un plan de acción para el cliente basado ESTRICTAMENTE "
            "en los procedimientos oficiales suministrados. No inventes configuraciones ajenas a estos artículos."
        )

        try:
            report_text = self._call_bedrock(prompt, image_base64)
        except Exception:
            report_text = self._generate_mock_report(diagnosis, articles)

        return {
            "status": "DEGRADED_RESOLVED",
            "diagnosis": diagnosis,
            "retrieved_articles": articles,
            "resolution_report": report_text
        }


def main():
    parser = argparse.ArgumentParser(description="Ring TelemetryOps Diagnostic Agent")
    parser.add_argument(
        "--video",
        type=str,
        default=os.path.join("data", "samples", "test_stream_good.mp4"),
        help="Ruta al archivo de vídeo de telemetría"
    )
    args = parser.parse_args()

    if not os.path.exists(args.video):
        print(f"[ERROR] No se encontró el archivo de vídeo en: {args.video}")
        return

    concierge = SupportConcierge()
    result = concierge.process_stream(args.video)
    print("\n" + result["resolution_report"])


if __name__ == "__main__":
    main()