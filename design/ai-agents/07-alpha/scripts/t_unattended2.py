import hlm, json
from t_unattended import case
case("RunNow service.restart allow", "service.restart", "service__restart", {"unit": "sshd"}, "allow")
case("RunNow ollama.remove allow", "ollama.remove_model", "ollama__remove_model", {"model": "llama3:8b"}, "allow")
case("RunNow journal.vacuum allow", "journal.vacuum", "journal__vacuum", {"days": 7}, "allow")
case("RunNow prune_stopped allow (medium, no unlock)", "docker.prune_stopped_containers", "docker__prune_stopped_containers", {"older_than_h": 1}, "allow")
case("RunNow prune dangling allow", "docker.prune_dangling_images", "docker__prune_dangling_images", {}, "allow")
case("RunNow files.list_older_than /etc (not watched)", "files.list_older_than", "files__list_older_than", {"path": "/etc", "days": 1}, "allow")
case("RunNow files.list_older_than traversal", "files.list_older_than", "files__list_older_than", {"path": "/tmp/../etc", "days": 1}, "allow")
case("RunNow act on lab2 not in task", "container.restart", "container__restart", {"name": "worker"}, "allow")
