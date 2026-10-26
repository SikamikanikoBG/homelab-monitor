Ето какво намерих и какво направих:

## Проблеми

| # | Проблем | Хост | Статус |
|---|---|---|---|
| 1 | **nginx.service failed** (exit 1) | local | ✅ **Рестартиран** |
| 2 | **logrotate.service failed** (exit 1) | vader | ❌ Не мога — policy е **Deny** за service.restart на vader |

## Направени действия
- ✅ `nginx.service` restart на **local** — успешно
- ✅ `docker.prune_dangling_images` на **local** — 44 images, 153 MB освободени
