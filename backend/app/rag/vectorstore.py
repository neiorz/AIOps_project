import logging
from typing import Optional
from pathlib import Path
import chromadb
from chromadb.config import Settings as ChromaSettings
from app.config import settings

logger = logging.getLogger(__name__)

class ChromaManager:
    _instance: Optional["ChromaManager"] = None
    _client: Optional[chromadb.ClientAPI] = None
    _collection = None

    COLLECTION_NAME = "devops_runbooks"

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ChromaManager, cls).__new__(cls)
            cls._instance._init_client()
        return cls._instance

    def _init_client(self):
        persist_dir = settings.CHROMA_PERSIST_DIR
        persist_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Initializing ChromaDB PersistentClient at {persist_dir}")
        self._client = chromadb.PersistentClient(
            path=str(persist_dir),
            settings=ChromaSettings(
                anonymized_telemetry=False,
                is_persistent=True,
            )
        )
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={"description": "DevOps runbooks, troubleshooting SOPs, and chaos post-mortems"}
        )
        logger.info(f"ChromaDB collection '{self.COLLECTION_NAME}' ready.")

    @property
    def client(self) -> chromadb.ClientAPI:
        if self._client is None:
            self._init_client()
        return self._client

    @property
    def collection(self):
        if self._collection is None:
            self._init_client()
        return self._collection

def get_chroma_manager() -> ChromaManager:
    return ChromaManager()
