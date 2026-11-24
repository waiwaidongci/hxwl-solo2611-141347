import logging
import time
from pathlib import Path
from src.services.execution import claim, execute, materialize_due
from src.services.triage_jobs import claim_recompute, execute_recompute
from src.utils.wakeup import wait

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
heartbeat = Path('/tmp/analysis-heartbeat')


def main():
    while True:
        try:
            materialize_due()
            lease = claim()
            if lease:
                execute(lease)
            else:
                triage_lease = claim_recompute()
                if triage_lease:
                    execute_recompute(triage_lease)
                else:
                    wait()
            heartbeat.touch()
        except Exception:
            log.exception('Worker cycle failed')
            time.sleep(2)


if __name__ == '__main__':
    main()
