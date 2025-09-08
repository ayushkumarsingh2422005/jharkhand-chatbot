from typing import List, Dict, Any

import numpy as np
import google.generativeai as genai

from backend import config
from backend.vectorstore import FaissStore


class EmbeddingsClient:
    def __init__(self, model_name: str, api_key: str):
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY not set. Please configure your environment.")
        genai.configure(api_key=api_key)
        self.model_name = model_name

    def embed(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 768), dtype=np.float32)
        resp = genai.embed_content(model=self.model_name, content=texts, task_type="retrieval_document")
        # The SDK returns { 'embedding': [...]} for single, and {'embeddings': [{'values': [...]}, ...]} for batch.
        if isinstance(resp, dict) and "embeddings" in resp:
            vectors = [np.array(item["values"], dtype=np.float32) for item in resp["embeddings"]]
        elif isinstance(resp, dict) and "embedding" in resp:
            vectors = [np.array(resp["embedding"], dtype=np.float32)]
        else:
            # Fallback: try attribute access
            try:
                vecs = getattr(resp, "embeddings", None)
                if vecs is None:
                    vecs = [getattr(resp, "embedding")]
                vectors = [np.array(getattr(v, "values", v), dtype=np.float32) for v in vecs]
            except Exception as e:
                raise RuntimeError(f"Unexpected embedding response format: {type(resp)}") from e
        # Ensure 2D
        arr = np.vstack(vectors)
        return arr


class GeminiClient:
    def __init__(self, model_name: str, api_key: str):
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY not set. Please configure your environment.")
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(model_name)

    def generate(self, prompt: str, max_output_tokens: int = 512) -> str:
        response = self.model.generate_content(prompt, generation_config={"max_output_tokens": max_output_tokens})
        return (response.text or "").strip()


class RAGPipeline:
    def __init__(self, store: FaissStore, embedder: EmbeddingsClient, llm: GeminiClient):
        self.store = store
        self.embedder = embedder
        self.llm = llm

    def build_index(self, chunks: List[Dict[str, Any]]) -> int:
        texts = [c["text"] for c in chunks]
        metas = [{k: v for k, v in c.items() if k != "text"} for c in chunks]
        if not texts:
            return 0
        vectors = self.embedder.embed(texts)
        self.store.add(vectors, metas)
        self.store.save()
        return vectors.shape[0]

    def retrieve(self, question: str, top_k: int) -> List[Dict[str, Any]]:
        q_vec = self.embedder.embed([question])[0]
        hits = self.store.search(q_vec, top_k)
        results: List[Dict[str, Any]] = []
        for score, meta in hits:
            results.append({
                "score": score,
                "text": meta.get("text", ""),
                "source_file": meta.get("source_file", ""),
                "page_start": int(meta.get("page_start", 0)),
                "page_end": int(meta.get("page_end", 0)),
            })
        return results

    def answer(self, question: str, top_k: int, max_output_tokens: int) -> Dict[str, Any]:
        retrieved = self.retrieve(question, top_k)
        context_blocks = []
        for i, r in enumerate(retrieved, start=1):
            header = f"[Source {i}] file: {r['source_file']} pages: {r['page_start']}-{r['page_end']} (score={r['score']:.3f})"
            context_blocks.append(header + "\n" + r["text"]) 
        context_text = "\n\n".join(context_blocks)
        prompt = (
            "You are a helpful assistant answering questions about Jharkhand government policies.\n"
            "Use ONLY the provided sources. If the answer is not contained, say you don't know.\n"
            "Cite sources inline as [Source N] where N corresponds to the source block.\n\n"
            f"Question: {question}\n\nSources:\n{context_text}\n\nAnswer:"
        )
        answer_text = self.llm.generate(prompt, max_output_tokens=max_output_tokens)
        citations = []
        for i, r in enumerate(retrieved, start=1):
            citations.append({
                "source_file": r["source_file"],
                "page_start": r["page_start"],
                "page_end": r["page_end"],
                "score": r["score"],
                "snippet": r["text"][:500],
            })
        return {"answer": answer_text, "citations": citations}
