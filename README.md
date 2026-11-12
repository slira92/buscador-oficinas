# Buscador de Oficinas

Busca oficinas en Portal Inmobiliario para una zona y un rango de m², las rankea y las publica en un dashboard.
Mismo motor que el buscador de locales comerciales, con criterios de oficinas.

## Requerimiento actual (`config.json` → `busquedas`)

| Zona | Operación | Pedido | "Calza" | "Cercano" (±15%) |
|---|---|---|---|---|
| Ciudad Empresarial (Huechuraba) | Arriendo | 700–1.000 m² | 700–1.000 | 595–1.150 |

La zona se reconoce por el texto "Ciudad Empresarial" en la dirección o el título, o por coordenadas dentro
de la caja `caja` (lat/lon). Lo que queda fuera de la zona no se muestra.

## Cómo funciona

1. Busca en la categoría "oficina" de la comuna con un rango de m² ampliado (±35%), por superficie útil y total.
2. Entra a cada ficha: m², estacionamientos, gastos comunes, piso, privados, baños, antigüedad, coordenadas,
   fotos, descripción y quién publica.
3. Clasifica:
   - **Habilitación** (título y descripción): habilitada, semi habilitada, planta libre, obra gruesa o sin info.
   - **Calce de m²**: calza, cercano o fuera.
   - **Quién publica**: dueño o corredor.
   - **Repetidos**: el mismo piso publicado por varios corredores se muestra una vez, con "También lo publican".
4. Calcula **UF/m²** y **costo total/m²** (arriendo + gastos comunes), y lo compara con la mediana de la zona.
   Corrige los avisos que publican el precio o los gastos comunes por m² en vez del total.
5. **Puntaje 0–100** (pesos en `config.json` → `pesos_puntaje`):
   - precio vs mercado (35%)
   - calce de m² (25%)
   - estacionamientos por m² (15%; 2,5 cada 100 m² o más = 100)
   - habilitación (15%)
   - edificio (10%; antigüedad y certificación LEED)

## Comandos

```bash
py buscador_oficinas.py                    # búsqueda completa
py buscador_oficinas.py --solo-dashboard   # recalcula con config.json sin scrapear
powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1               # automático 09:30, 13:30, 17:30
powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1 -Desinstalar
```

## Publicar en un link

`config.json` → `publicar_github`: sube `dashboard.html` a la rama `gh-pages` del repo después de cada pasada.
