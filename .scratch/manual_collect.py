import asyncio

from src.bootstrap import build_infrastructure
from src.config_loader import load_config
from src.ingestion.models import CollectionTrigger
from src.jobs.ingestion import NORMAL_COLLECTION_PRIORITY, enqueue_source_scan
from src.runtime import install_runtime

SOURCE_IDS = [1, 2, 3, 4, 5]


async def main() -> None:
    config = load_config()
    infra = await build_infrastructure(config.database)
    install_runtime(infra)
    try:
        for source_id in SOURCE_IDS:
            job_id = await enqueue_source_scan(
                source_id=source_id,
                trigger=CollectionTrigger.MANUAL,
                priority=NORMAL_COLLECTION_PRIORITY,
            )
            print(f"source {source_id}: queued job {job_id}")
    finally:
        await infra.close()


asyncio.run(main())
