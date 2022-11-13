# Flask-SQLAlchemy example

This is a quick example of how to use `alchemical_queues.tasks` in a Flask context. We'll use the file `tasks.py` from the main tutorial:

```python
from alchemical_queues.tasks import task

@task
def add_numbers(taskinfo, a, b):
    print(f"Running {a}+{b}")
    return a + b
```

The minimal Flask app looks like this:

```python
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from alchemical_queues import AlchemicalQueues
from tasks import add_numbers


db = SQLAlchemy()
app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///project.db"
db.init_app(app)

queues = AlchemicalQueues()

with app.app_context():
    db.create_all()
    queues.set_engine(db.engine)
    queues.create_all()


@app.route("/<int:a>/<int:b>")
def add_some_numbers(a: int, b: int):
    task_queue = queues.get("task-queue")
    entry = add_numbers(a, b).schedule(task_queue)
    return f"task: {entry.entry_id}"


@app.route("/result/<int:entry>")
def result(entry: int):
    task_queue = queues.get("task-queue")
    task = add_numbers.retrieve(task_queue, entry)

    if task.result is None:
        return "No result yet"
    else:
        return f"result: {task.result}"


if __name__ == "__main__":
    app.run()
```

The worker can be run like this:

```bash
alchemical_worker "sqlite:///project.db" task-queue
```