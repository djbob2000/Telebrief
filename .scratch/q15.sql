\pset pager off
SELECT id, status, queue_name, priority, lock, queueing_lock, attempts, scheduled_at, args FROM procrastinate.procrastinate_jobs WHERE task_name='process_event_revisions' AND status IN ('todo','doing') ORDER BY id LIMIT 12;
SELECT 'todo_by_lock', coalesce(lock,'<null>'), count(*) FROM procrastinate.procrastinate_jobs WHERE task_name='process_event_revisions' AND status='todo' GROUP BY 2;
SELECT 'doing_all_queues', queue_name, task_name, count(*) FROM procrastinate.procrastinate_jobs WHERE status='doing' GROUP BY 2,3;
SELECT 'workers', id, last_heartbeat, queues FROM procrastinate.procrastinate_workers;
