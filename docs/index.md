# Welcome to Alchemical Queues

Have you got a small web application with a couple of users and a database accessed via SQLAlchemy? Do you need to run a couple tasks in the background but does it feel like complete overkill to set up a Celery-based system and have to run a broker like Redis or RabbitMQ just for your three automated emails you send per day? Then you are the target audience of *Alchemical Queues*.

*Alchemical Queues* is a small project that implements safe distributed queues on top of SQLAlchemy. On top of that is an implementation of task queues for which you can run one or more workers. Because it only has one dependency (SQLAlchemy) most likely you are just adding ~500 lines of python to your deployment with no additional external services required.

## Features

- **Zero Infrastructure**: No Redis, RabbitMQ, or other brokers needed - just your existing database
- **Thread and Process Safe**: Concurrent access from multiple workers is fully supported
- **Priority Queues**: Schedule tasks with different priorities
- **Delayed Execution**: Schedule tasks to run at a specific time
- **Multiple Databases**: Works with PostgreSQL, MySQL, SQLite, Oracle, and any SQLAlchemy-supported database
  - **NOTE**: only PostgreSQL and SQLite are tested regularly in CI!
- **Type Safe**: Full type hints and mypy support
- **Lightweight**: Single dependency (SQLAlchemy), ~500 lines of code

## Quick Example

```python
from sqlalchemy import create_engine
from alchemical_queues import AlchemicalQueues

# Use your existing database
engine = create_engine("postgresql://user:pass@localhost/myapp")
queues = AlchemicalQueues(engine)
queues.create_all()

# Simple queue usage
queue = queues.get("emails")
queue.put({"to": "user@example.com", "subject": "Hello"})

# Or use the task queue
from alchemical_queues.tasks import task

@task
def send_email(taskinfo, to, subject):
    # Your email sending logic
    pass

task_queue = queues.get("tasks")
send_email("user@example.com", "Hello").schedule(task_queue)
```

## When to Use

**Good fit:**
- Small to medium web applications
- Background tasks with low to medium volume
- Applications that already use SQLAlchemy
- Development and testing environments
- When you want to minimize infrastructure complexity

**Not recommended:**
- High-throughput, high-performance requirements (use Celery + Redis)
- Need advanced features like routing, chaining, callbacks
- Real-time processing with sub-second latency requirements

## Getting Started

Check out the [Installation Guide](usage/installation.md) and [Tutorial](usage/tutorial.md) to get started!
