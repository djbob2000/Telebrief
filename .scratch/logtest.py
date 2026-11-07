import logging, sys
sys.path.insert(0, "/app")
from src.utils import setup_logging
setup_logging("INFO")
logging.getLogger("src.jobs.event_processing").info("LOGTEST src module info line")
logging.getLogger("telebrief.worker").info("LOGTEST telebrief info line")
import src.jobs.event_processing as ep
print("effective", logging.getLogger("src.jobs.event_processing").getEffectiveLevel(), ep.logger.name, ep.logger.disabled, ep.logger.propagate, flush=True)
