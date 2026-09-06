# multi-model-agentflow

[English](README.md) | [简体中文](README.zh-CN.md) | [Español](README.es.md) | [Português](README.pt.md)

`multi-model-agentflow` es un plano de control independiente de plataforma y dominio, sensible al riesgo, para coordinar múltiples modelos de IA durante la planificación, autorización, ejecución, verificación, revisión, seguimiento de costos, controles de privacidad y recuperación.

El proyecto se basa en una idea central: los modelos deben generar, implementar y revisar dentro de límites explícitos, mientras que el software determinista se encarga de la autorización, el estado, los presupuestos, las compuertas de seguridad, la evidencia y la recuperación.

## Por Qué Existe

Los flujos complejos con agentes pueden fallar de formas difíciles de auditar:

- un plan cambia, pero el límite de autorización no cambia con él;
- un modelo escribe, prueba, revisa y declara completo su propio trabajo;
- los modelos remotos reciben más contexto o datos sensibles de lo necesario;
- las llamadas pagadas se reintentan después de fallos inciertos;
- las tareas largas se interrumpen sin un estado claro de recuperación;
- las ediciones humanas invalidan algunos resultados, pero no necesariamente todos;
- el estado queda disperso entre chat, terminales, logs, salidas de modelos y diffs de Git.

`multi-model-agentflow` intenta hacer que esos flujos sean explícitos, inspeccionables y recuperables.

## Estado Actual

El MVP local está implementado y validado con pruebas automatizadas. Cubre el ciclo principal:

```text
plan -> authorize -> implement -> self-test -> deterministic gates
-> independent review -> revise/rereview -> approve
```

También cubre pausa segura, congelamiento inmediato, toma de control humana, ejecución recuperable, seguimiento idempotente de llamadas, evidencia de costos, controles de límites de archivos y evidencia de revisión.

Los roles remotos basados en OpenCode están incluidos en el alcance actual:

- Reviewer remoto: solo lectura, solo packet, sin acceso al workspace del repositorio;
- Worker remoto: rol de implementation/revision, solo cuando el contrato de tarea lo permite explícitamente;
- la ejecución del Worker remoto ocurre dentro de un staging sandbox mínimo;
- las entradas del Worker remoto se declaran mediante `input_artifacts` y se verifican con SHA-256;
- las salidas del Worker remoto se sincronizan solo hacia `allowed_files`, con límites de todo o nada;
- las llamadas remotas están restringidas por hash del plan, snapshot de autorización, presupuesto, política de privacidad, alcance de archivos, permisos de rol y política de red.

El desarrollo y la validación por defecto usan test doubles sin costo, rutas de modelos locales, procesos simulados y ejecutables OpenCode stub. Las llamadas reales a providers pagados y los smoke tests remotos reales requieren un plan separado, hash mostrado, aprobación explícita y autorización.

## Garantías Principales

- **Activación explícita**: el descubrimiento automático de Skills puede sugerir AgentFlow, pero no autoriza iniciar modelos.
- **Autorización por hash de plan**: la autorización se vincula al JSON canónico del plan y a un hash SHA-256 del contenido.
- **Invalidación por cambio de alcance**: cambiar modelos, archivos, presupuesto, exposición de datos, permisos o efectos secundarios requiere nueva autorización.
- **Riesgo en dos ejes**: la importancia de negocio usa B0-B3; la seguridad operativa usa S0-S3.
- **Compuertas de privacidad**: los datos se clasifican como D0-D3; los datos D3 y los secretos nunca van a remoto.
- **Separación de roles**: implementación, self-test y revisión no pueden colapsar en una ruta de autoaprobación por un solo modelo.
- **Revisión independiente**: los Reviewers usan contexto nuevo y de solo lectura; el trabajo importante o crítico requiere revisión entre familias de modelos.
- **Aislamiento del Reviewer remoto**: los Reviewers remotos reciben solo un Review Packet mínimo, no el workspace del repositorio.
- **Sandbox del Worker remoto**: los Workers remotos se ejecutan en un staging sandbox mínimo con red denegada por defecto.
- **Snapshots de entrada**: las entradas del Worker remoto son archivos relativos al proyecto verificados con SHA-256.
- **Sincronización atómica de salidas**: las salidas del Worker remoto se copian de vuelta solo dentro de archivos autorizados y solo cuando pasan los controles de sincronización.
- **Política de aceptación de revisión**: las tareas pueden usar `block_p0_p1` o `zero_findings`.
- **Honestidad de costos**: las llamadas registran tokens, duración, costo, estado de costo no disponible y claves de idempotencia.
- **Estado determinista**: eventos, proyecciones, llamadas, pruebas, revisiones y evidencia viven en una única fuente de estado SQLite.
- **Checkpoints de supervisor**: los eventos deterministas de activación pueden crear supervisor checkpoints acotados para supervisión de bajo consumo de tokens.
- **Pausa y recuperación**: las ejecuciones pueden pausarse, congelarse, tomarse y reanudarse sin repetir llamadas pagadas completadas.
- **Plano de control opcional**: AgentFlow puede detenerse; los Git worktrees, diffs y evidencias siguen disponibles para trabajo manual.

## Inicio Rápido

Ejecutar pruebas:

```bash
pytest
```

Mostrar el plan actual:

```bash
agentflow plan show
```

Autorizar un hash de plan mostrado:

```bash
agentflow plan authorize --hash <sha256>
```

Iniciar una ejecución:

```bash
agentflow start <plan-id>
```

Inspeccionar estado:

```bash
agentflow status <run-id>
```

Seguir estado y logs:

```bash
agentflow status <run-id> --watch
agentflow logs <run-id> --follow
```

Inspeccionar evidencia de costos:

```bash
agentflow cost <run-id>
```

Leer o registrar checkpoints de supervisor:

```bash
agentflow supervisor-next <run-id>
agentflow supervisor-record <run-id> <checkpoint-id> --decision '<json>'
```

Pausar, congelar, tomar control, resolver, reanudar o cancelar:

```bash
agentflow pause <run-id>
agentflow pause <run-id> --immediate
agentflow takeover <run-id>
agentflow handoff <run-id>
agentflow resolve-call <run-id> <call-id>
agentflow resume <run-id>
agentflow cancel <run-id>
```

Ejecutar contra una raíz de proyecto explícita:

```bash
agentflow --project <root> status <run-id>
```

## Flujo Típico

1. Crear o generar `.agentflow/plan.json`.
2. Ejecutar `agentflow plan show`.
3. Revisar el plan normalizado, alcance efectivo, modelos, archivos, presupuesto, política de privacidad, modo, vencimiento y SHA-256.
4. Aprobar explícitamente el hash mostrado.
5. Ejecutar `agentflow plan authorize --hash <sha256>`.
6. Ejecutar `agentflow start <plan-id>`.
7. Observar mediante `status`, `logs`, `cost` y `supervisor-next`.
8. Usar pausa segura, congelamiento, toma de control o reanudación cuando se necesite intervención humana.
9. Aprobar la finalización solo después de que pasen las compuertas deterministas y la revisión independiente.

## Roles Remotos

### Reviewer

Un Reviewer remoto solo puede ejecutar `review` o `rereview`.

Es de solo lectura por defecto, no recibe el workspace del repositorio y solo recibe el Review Packet mínimo generado por AgentFlow. El Review Packet está sujeto a compuertas de privacidad, presupuesto, autorización e independencia.

Un Reviewer remoto no debe editar archivos, escribir en disco, ejecutar comandos shell, acceder a directorios externos, navegar la web, invocar Skills, iniciar tareas/subagentes ni solicitar upgrades interactivos de permisos.

La salida del Reviewer debe ser un único objeto JSON válido según el protocolo. Prosa no JSON, JSON en fences, campos faltantes, tipos inválidos, severidades inválidas o aprobaciones contradictorias no pueden aprobar una tarea.

### Worker

Un Worker remoto puede ejecutar `implementation` o `revision` solo cuando el contrato de tarea establece explícitamente:

```yaml
allow_remote_implementation: true
```

El Worker se ejecuta dentro de un staging sandbox mínimo que contiene solo:

- `input_artifacts` de solo lectura verificados por hash;
- `allowed_files` autorizados;
- un briefing de tarea generado.

El acceso de red se deniega por defecto. Actualmente, las allowlists de hosts no pueden expresarse de forma segura mediante la capa de permisos de OpenCode, por lo que el modo allowlist falla cerrado.

Las salidas del Worker se sincronizan de vuelta solo hacia `allowed_files`. La sincronización usa controles de baseline y manifest y sigue un límite de todo o nada. Mutación de entradas, salida fuera de alcance, conflicto de destino, manifest dañado o rollback fallido bloquean la integración.

## Contrato de Tarea

Un contrato de tarea describe el límite de ejecución antes de que corra cualquier modelo:

```yaml
task_id:
objective:
risk_level:
allowed_files:
forbidden_actions:
acceptance_criteria:
data_sensitivity:
implementation_model:
review_model:
fallback_model:
max_remote_cost:
max_retry_count:
escalation_conditions:
expected_outputs:
implementation_max_steps:
implementation_timeout_seconds:
implementation_max_continuations:
allow_remote_implementation:
remote_worker_network_mode:
remote_worker_allowed_hosts:
remote_worker_max_steps:
remote_worker_timeout_seconds:
input_artifacts:
review_acceptance_policy:
```

`risk_level` contiene tanto la importancia de negocio como la seguridad operativa. El contrato de tarea forma parte del JSON canónico del plan y del hash de autorización.

## Modos de Operación

- **Managed**: el plan aprobado puede ejecutarse dentro de su alcance autorizado sin confirmaciones adicionales.
- **Supervised**: cada llamada de implementation, review, revisión y rereview requiere confirmación.
- **Adaptive**: las confirmaciones se determinan solo por los umbrales B/S estáticos del plan y los marcadores de nodos críticos.

El modo Adaptive no realiza enrutamiento basado en aprendizaje ni cambia silenciosamente la estrategia.

## Estado y Recuperación

AgentFlow guarda el estado de ejecución en una base SQLite local. Los eventos y las proyecciones de estado actual se actualizan en la misma transacción, por lo que la recuperación puede distinguir trabajo completado, trabajo fallido, llamadas desconocidas, revisión pendiente y estados de intervención humana.

Si una llamada pagada o remota tiene un resultado incierto, AgentFlow la registra como `UNKNOWN` y pausa para conciliación. No debe reintentarla de forma especulativa.

Si una llamada local de implementation o revision alcanza un límite de pasos conocido, se registra como un fallo incompleto conocido. Puede continuar solo como un segmento autorizado de la misma sesión y solo dentro de `implementation_max_continuations`. Los Reviewers remotos y Workers remotos no continúan de esta manera.

## Mapa de Documentación

- `docs/requirements.md`: requisitos normativos del producto con IDs estables y evidencia de aceptación.
- `docs/mvp.md`: alcance del MVP, no objetivos, escenarios de aceptación y estado de validación.
- `docs/architecture-decisions.md`: decisiones de arquitectura aceptadas, tentativas y pendientes.
- `task_plan.md`: hitos de implementación y plan de trabajo actual.
- `progress.md`: notas de progreso y registros de validación.
- `skills/multi-model-agentflow/SKILL.md`: punto de entrada de Codex Skill.
- `AGENTS.md`: reglas de colaboración del repositorio y límites de seguridad.

## Límites de Seguridad

Por defecto, este proyecto no:

- descarga ni instala modelos;
- registra cuentas de providers;
- compra créditos;
- recopila ni guarda secretos de providers;
- llama modelos fuera de la autorización del plan;
- envía datos D3, secretos, tokens o datos privados de usuario a modelos remotos;
- trata modelos OpenCode configured/discoverable como modelos callable-verified;
- permite que Reviewers remotos editen archivos, ejecuten shells, naveguen o lean el workspace del repositorio;
- permite que Workers remotos usen red, shell, directorios externos, Skills, tareas, subagentes o upgrades interactivos;
- usa prosa de modelos como sustituto de pruebas, registros de revisión, evidencia de base de datos o diffs de Git;
- usa modelos de IA para implementar la máquina de estados, locks, controles de presupuesto, lógica de espera o compuertas deterministas;
- genera costos de API sin autorización explícita.

## Notas de Desarrollo

El núcleo genérico debe permanecer independiente del proveedor de modelos, agent engine, plataforma y dominio de aplicación. La política específica de dominio pertenece a la configuración del proyecto, la documentación del proyecto o la estrategia proporcionada por quien llama.

Los nuevos requisitos deben agregarse a `docs/requirements.md` con IDs estables, comportamiento observable y evidencia de aceptación. Los cambios de alcance del MVP deben reflejarse en `docs/mvp.md`. Las decisiones de arquitectura y preguntas abiertas deben registrarse en `docs/architecture-decisions.md`.

Los cambios de implementación deben preservar evidencia de pruebas, revisión, costos, autorización, estado y recuperación. Los test doubles deben estar claramente etiquetados y no deben presentarse como ejecuciones reales de modelos.

## Descargo de Responsabilidad

Este proyecto es software experimental para flujos de investigación y desarrollo. Se proporciona tal cual, sin garantía de ningún tipo.

No ofrece asesoramiento legal, financiero, de seguridad, cumplimiento ni otro asesoramiento profesional. Los usuarios son responsables de revisar planes, permisos, salidas de modelos, costos, límites de privacidad, comportamiento de providers y efectos posteriores antes de usarlo en proyectos reales o con providers reales de modelos.

Las llamadas reales a providers remotos, el uso de modelos pagados, el procesamiento de datos sensibles y el despliegue en producción requieren revisión separada y autorización explícita.

## Licencia

Este proyecto está licenciado bajo Apache License 2.0. Consulta `LICENSE` para más detalles.
