# Verificação da integração Floodcast / ECMWF / SYMFLUENCE

> **Nota (histórica / pending re-validation).** Este relatório foi produzido pelo contribuidor
> antes da migração para o SUMMA-mizuRoute acoplado (SUMMA `develop`, `USE_MIZUROUTE=ON`) e
> refere-se a um repositório completo que **não** corresponde a este checkout: vários módulos
> de dados nunca foram submetidos (ver [`../KNOWN_MISSING_MODULES.md`](../KNOWN_MISSING_MODULES.md)).
> As contagens abaixo não são reproduzíveis a partir deste checkout.


Data: **17 de novembro de 2026**. Execução sobre um clone completo do Floodcast,
base `39cd5511a0f85d778e11ec85e0289364449c4b15`, com as alterações já integradas.
O SYMFLUENCE foi obtido no commit exato
`df9b6447080b85ec6566dc7ad4ed901a623ad0bc`.

## Resultado

| Verificação | Resultado observado |
| --- | --- |
| Suite configurada `tests/unit` do repositório completo | **807 passed, 2 subtests passed**, 0 falhas, 0 skips; 58,23 s |
| Suite adicional `tests/integration` | **8 passed, 79 skipped**, 0 falhas; 0,46 s |
| Ruff: `src`, `scripts`, `tests`, `tools` e novo exemplo offline | Passou |
| `git diff --check` | Passou |
| Consistência do `pixi.lock` com o projeto | Passou com Pixi 0.84.1 |
| Build isolado wheel e sdist | Passou com o backend `setuptools==82.0.1` |
| Instalação do wheel em diretório separado e planos pela CLI/API | Passou para IFS, S2S, sazonal e exemplo IFS→SUMMA |
| Verificação dos fontes originais e ports | 903 originais + 2 suplementares, 205 módulos `data`, 18 ports, 2 diffs e licença verificados |
| Aquisição IFS real + descodificação ecCodes + normalização | Passou para a seleção documentada abaixo |
| Workflow IFS real até forcing SUMMA e retoma | Quatro steps passaram; registo terminal conservado |
| Remapeamento contra interseção independente | Passou nas tolerâncias declaradas |
| Conservação temporal de precipitação e energia | Passou na amostra real e nos casos sintéticos |
| S2S e sazonal: GRIB nativo sintético → canónico → HRUs | Passou, com transporte substituído e ecCodes/EASYMORE reais |
| Exemplo offline distribuído | Quatro steps passaram com a amostra IFS real incluída |

Os 79 skips não são testes aprovados. Correspondem a verificações que exigem
imagens Docker explicitamente selecionadas, outputs reais de modelos ou a
ativação deliberada dos wrappers/Compose. Não foram executados Docker, SUMMA,
FLUXOS, mizuRoute (agora acoplado ao SUMMA) ou HPC nesta entrega.

As duas suites são distintas. O total é **815 testes aprovados, 2 subtestes
aprovados e 79 testes não executados**. As contagens dos subconjuntos executados
pelos agentes não foram somadas novamente. Os ficheiros JUnit
[`unit-tests.xml`](unit-tests.xml) e [`integration-tests.xml`](integration-tests.xml)
conservam os resultados individuais; o JUnit conta os dois subtestes como casos
adicionais, pelo que indica 809 casos na suite unitária.

## Ambiente e alcance da regressão

O ambiente de execução foi Linux x86-64 com Python **3.12.14**, Pydantic **2.14.2**
e filelock **3.32.7**. As dependências centrais correspondem às versões fixadas
no `pyproject.toml`. O ambiente Conda/Pixi seleciona Python **3.12.16**; esse
patch exato do interpretador não foi utilizado. Windows e macOS não foram
executados. A lista completa de versões está em
[`environment.json`](environment.json).

A suite unitária utiliza os ficheiros reais do Stage 1 existentes no clone.
Não foram usados os pequenos substitutos de Stage 1 presentes na validação do
ZIP anterior. Os testes incluem contratos, registos, CLI, instaladores e runtime
existentes, o núcleo Stage 2 e os novos percursos de dados. Casos que usam
substitutos de clientes externos são identificados nos testes; não são
apresentados como aquisição autenticada real.

Os 11 avisos da suite unitária foram avisos de depreciação de pytest/AnyIO e
EASYMORE e um aviso esperado da fixture sem CRS usada para testar rejeição.
A suite de integração produziu um aviso de depreciação pytest/AnyIO.

### Correção de portabilidade encontrada na regressão adicional

O primeiro ensaio de `tests/integration` encontrou uma falha no gerador de
perfis hidrológicos existente: `sorted(Path)` produz ordens diferentes para
maiúsculas/minúsculas em Windows e POSIX. O código do commit base reproduziu a
mesma falha. Foram comparados os 77 ficheiros referenciados, incluindo os hashes,
e a única diferença JSON era a ordenação dos ficheiros nos três checks de
referência.

Foi acrescentada uma chave explícita, por componentes com `casefold()` e
desempate por componentes originais, em
`tests/integration/hydrology/generate_profile.py`. O perfil versionado, os
inputs, os hashes e as expectativas científicas ficaram intactos. A suite
adicional voltou a executar com 8 aprovações e 79 skips.

## O que o ensaio IFS real demonstrou

| Campo | Seleção efetivamente executada |
| --- | --- |
| Inicialização | 2026-11-17 00:00 UTC |
| Produto e resolução | IFS Open Data, `0p25` |
| Membros | Controlo nativo `0` e perturbado `1` |
| Variáveis | `2t`, `2d`, `10u`, `10v`, `sp`, `tp`, `ssrd`, `strd` |
| Lead times | Instantâneos 0/3/6 h; acumulações 3/6 h |
| Aquisição original | 42 mensagens GRIB, 16 assets, 20 102 931 bytes |
| Subconjunto espacial | 7 × 9 pontos, Portugal continental |
| HRUs do ensaio independente | Dois retângulos sintéticos, IDs 501 e 502 |
| Saída de forcing | Seis intervalos horários, sete variáveis SUMMA, um NetCDF e uma file list por membro |
| Revisão de forcing | `ffw-summa-forecast-forcing-v2-20261117` |

O SDK `ecmwf-opendata` fez a aquisição; ecCodes descodificou os GRIBs reais.
A execução completa posterior reutilizou esses inputs, gerou pesos e forcing
num diretório novo e rejeitaria qualquer tentativa de novo download. Os quatro
steps terminaram e a retoma devolveu o mesmo registo terminal.

| Membro | HRU sintética | Precipitação 0–6 h antes (mm) | Depois (mm) |
| --- | ---: | ---: | ---: |
| 0 | 501 | 7,906657225 | 7,906657225 |
| 0 | 502 | 10,009839025 | 10,009839025 |
| 1 | 501 | 6,496309815 | 6,496309815 |
| 1 | 502 | 8,765051281 | 8,765051281 |

A diferença absoluta observada dos integrais foi zero nos quatro casos. A
energia de onda longa também foi conservada. A radiação solar nesta amostra
noturna era zero; a conservação de fluxos solares não nulos é coberta por
testes sintéticos separados.

A comparação com um cálculo independente das interseções em EPSG:6933 deu
diferença absoluta máxima de `1.4526143200625785e-11 kg m-2 s-1` e relativa
máxima de `3.1226985441890065e-8`, dentro de `rtol=2e-3`,
`atol=1e-10 kg m-2 s-1`. Os valores, critérios, hashes e software estão em
[`live-ifs.json`](live-ifs.json) e a descrição em
[`live-ifs.md`](live-ifs.md).

O ensaio não representa uma bacia calibrada. A interpolação linear dos campos
instantâneos e a redistribuição uniforme dos fluxos foram opções explícitas.
A conservação dos integrais não demonstra conhecimento da variabilidade
meteorológica dentro dos intervalos nativos.

## Reprodução offline e separação por produto

O repositório inclui uma pequena extração espacial dos dados reais IFS em
`tests/fixtures/ecmwf/ifs_20261117T00Z`. A recodificação GRIB IEEE64 preserva
exatamente os valores descodificados selecionados. O `provenance.json` da
fixture contém hashes dos originais, dos ficheiros derivados e dos valores,
pedidos e atribuição ECMWF/CC BY 4.0.

`examples/workflows/ifs_data_offline_demo.py` executa as mesmas implementações
de descodificação, pré-processamento, remapeamento e forcing com transporte
substituído. As suas HRUs 101/202 são exemplos sintéticos próprios, distintos
dos retângulos 501/502 do verificador independente acima.

Para S2S e sazonal, os testes geraram mensagens GRIB com ecCodes, executaram
a descodificação, a normalização e o EASYMORE. Verificaram limites de 24 h,
médias diárias, membros nativos, unidades, sistema sazonal e seleção local.
**Não foi feita uma aquisição real autenticada ECDS/CDS.** Os testes não
validam credenciais, permissões da conta, filas remotas ou mudanças de catálogo.

O forcing SUMMA rejeita pressão de superfície ausente e temperaturas diárias
incompatíveis com a política temporal solicitada. O êxito do pré-processamento
e remapeamento S2S/sazonal não implica forcing SUMMA completo para esses
subconjuntos. Não é usada pressão ao nível do mar como substituto de pressão
de superfície e não se inventam campos em falta.

## Reutilização, distribuição e integridade

O verificador `tools/verify_source_reuse.py` conferiu os 903 originais do
snapshot e dois originais suplementares, os 205 módulos Python de `data`,
18 ports, dois diffs, avisos e GPL. Testes negativos confirmaram que um byte
runtime alterado causa erro e impede a regeneração dos diffs; um diff
alterado também causa erro. A execução é offline e usa apenas a biblioteca
padrão Python. Ver [`docs/source-reuse.md`](../source-reuse.md).

O wheel foi instalado num diretório separado. Os imports e os quatro planos
CLI/API vieram dessa instalação, não do checkout editável. A prova está em
[`wheel-smoke.json`](wheel-smoke.json). O build isolado inclui licenças no wheel
e fontes, exemplos, fixtures, ferramentas e vendor no sdist. O arquivo final
do repositório inclui ainda toda a árvore original do Floodcast e as alterações
integradas. A verificação de distribuição e do ZIP extraído está registada em
[`package-verification.json`](package-verification.json).

O `pixi.lock` foi atualizado para refletir os requisitos opcionais; o conjunto
resolvido dos ambientes default/dev não mudou. Esta atualização não cria um
lock multiplataforma de todo o extra `forecast`.

## Comandos de reprodução

Num checkout extraído, com dependências de desenvolvimento e forecast:

```bash
python -m pip install -e '.[forecast]' --group dev
python -B -m pytest -q -p no:cacheprovider
python -B -m pytest tests/integration -q -p no:cacheprovider
ruff check src scripts tests tools --no-cache
python tools/verify_source_reuse.py
python -m build
python examples/workflows/ifs_data_offline_demo.py --output data/ifs_demo
```

`--group dev` requer uma versão de pip que suporte dependency groups. Em
alternativa, instalar explicitamente as ferramentas de desenvolvimento declaradas
em `pyproject.toml`. Os testes opt-in de integração continuarão como skips até
serem fornecidos os recursos/flags requeridos; não os interpretar como aprovações.

## Limites que permanecem

- Execução de SUMMA-mizuRoute (acoplado) e FLUXOS, simulações de bacia e validação
  hidrológica: **não executadas**.
- Estado físico T0, calibração, limiares por troço, probabilidades de cheia,
  fronteiras FLUXOS e produto de aviso: fora deste incremento de dados.
- Remapeamento restrito a grelhas regulares, geometrias válidas e cobertura
  verificada; sem reparação automática, remapeamento antimeridiano, correção
  altimétrica ou de viés.
- Reconciliação persistente de jobs remotos ECDS/CDS após perda de ligação:
  não implementada; consultar a documentação antes de repetir submissões.
- As versões e amostras testadas delimitam a evidência. Não constituem garantia
  sobre todos os membros, ciclos, campos, horizontes ou mudanças futuras ECMWF.
- Não houve push ou pull request. O conector GitHub disponibilizava leitura,
  sem permissão de escrita no repositório de destino.
