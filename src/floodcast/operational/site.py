"""Static public website for the operational flood forecast.

The site is generated only from the files described in
``docs/operational/outputs.md``. Output is plain HTML/CSS/JS with inline SVG
charts (no build step, no cookies, no trackers, no third-party fonts). The only
external requests are Leaflet (cdnjs, pinned + SRI) and OpenStreetMap tiles, and
only when a flood-depth map exists.
"""

from __future__ import annotations

import csv
import html
import json
import os
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["DEPTH_CLASSES", "LEVELS", "build_site"]

ASSET_DIR = Path(__file__).parent / "site_assets"
MAX_CYCLE_PAGES = 30
COPY_SUFFIXES = {".json", ".csv", ".png", ".geojson", ".html", ".jpg", ".js"}

LEVELS = ("none", "watch", "warning", "alert", "unknown")

# Depth classes (lower m, upper m or None, colour). The hydraulics step should
# render max_depth.png with this ramp so that the map legend is truthful.
DEPTH_CLASSES = (
    (0.1, 0.5, "#9ecae1"),
    (0.5, 1.0, "#4292c6"),
    (1.0, 2.0, "#2171b5"),
    (2.0, None, "#08306b"),
)

# key -> (pt, en)
T: dict[str, tuple[str, str]] = {
    "nav_forecast": ("Previsão", "Forecast"),
    "tagline": ("Previsão experimental de cheias · Rio Sado, Alcácer do Sal", "Experimental flood forecast · Sado river, Alcácer do Sal"),
    "jump": ("Ir para", "Jump to"),
    "j_summary": ("Resumo", "Summary"),
    "j_rain": ("Chuva", "Rainfall"),
    "j_flow": ("Caudal", "River flow"),
    "speed_l": ("Velocidade", "Speed"),
    "speed_pause": ("Pausa", "Pause"),
    "speed_p": ("Os gráficos, os mapas e a animação 3D abaixo mostram as próximas 72 h em movimento, todos sincronizados no mesmo instante "
                "(a linha vertical). Estes botões mudam a velocidade dessa animação: ❚❚ pára, ½× mais devagar, 1× normal (72 h em 30 s), "
                "2× e 4× mais depressa. Para ir a um instante concreto, carregue num gráfico e arraste para a frente ou para trás.",
                "The charts, maps and the 3-D animation below play the next 72 h, all synchronised on the same moment (the vertical line). "
                "These buttons set the speed of that animation: ❚❚ pauses, ½× slower, 1× normal (72 h in 30 s), 2× and 4× faster. "
                "To go to a given moment, press on a chart and drag forwards or backwards."),
    "j_flood": ("Áreas inundadas", "Flooded areas"),
    "j_anim": ("Animação 3D", "3-D animation"),
    "j_official": ("Fontes oficiais", "Official sources"),
    "j_data": ("Dados", "Data"),
    "kpi_rain": ("Chuva máxima em 72 h (média da bacia)", "Max 72-h rainfall (basin mean)"),
    "kpi_flow": ("Caudal de pico previsto (mediana)", "Forecast peak flow (median)"),
    "kpi_prob": ("Probabilidade de exceder o nível de aviso", "Chance of exceeding the warning flow"),
    "kpi_flood": ("Área inundada (cenário P90)", "Flooded area (P90 scenario)"),
    "kpi_depth": ("profundidade máx. {d} m", "max depth {d} m"),
    "kpi_range": ("intervalo {lo}–{hi} {u}", "range {lo}–{hi} {u}"),
    "kpi_worst": ("Cenário superior (pior plausível)", "Upper scenario (plausible worst)"),
    "ens_band": ("Conjunto multi-fonte P10–P90 (acumulada)", "Multi-source ensemble P10–P90 (cumulative)"),
    "scn_lower": ("Inferior", "Lower"),
    "scn_central": ("Central", "Central"),
    "scn_median": ("Mediana", "Median"),
    "scn_upper": ("Superior", "Upper"),
    "scn_h": ("Cenários de chuva e caudal de ponta", "Rainfall scenarios and peak flow"),
    "scn_col": ("Cenário", "Scenario"),
    "scn_peak_txt": ("Cenário central: pico às {t}. A faixa mostra os cenários inferior e superior.",
                     "Central scenario: peak at {t}. The band spans the lower and upper scenarios."),
    "hydro_p_scn": ("Caudal simulado com três cenários de chuva — inferior (P10), central (P50) e superior (P90) da chuva acumulada de todas as previsões disponíveis.",
                    "Flow simulated with three rainfall scenarios — lower (P10), central (P50) and upper (P90) of the cumulative rain of all available forecasts."),
    "src_tbl_h": ("Estado de cada fonte de previsão de chuva", "Status of each rainfall forecast source"),
    "src_h": ("Previsões de chuva utilizadas", "Rainfall forecasts used"),
    "src_p": ("{n} previsões de vários centros meteorológicos, ordenadas pela chuva acumulada na bacia. Os três cenários são previsões reais:",
              "{n} forecasts from several weather centres, ranked by cumulative basin rain. The three scenarios are real forecasts:"),
    "lk_btn": ("Probabilidade", "Likelihood"),
    "lk_vl": ("Muito provável", "Very likely"),
    "lk_l": ("Provável", "Likely"),
    "lk_p": ("Possível", "Possible"),
    "lk_note": ("Muito provável: inundado mesmo no cenário inferior · Provável: no central · Possível: só no superior.",
                "Very likely: flooded even in the lower scenario · Likely: in the central · Possible: only in the upper."),
    "tide_h": ("Nível do mar na foz do Sado (maré + sobrelevação)", "Sea level at the Sado mouth (tide + surge)"),
    "tide_lvl": ("Nível do mar", "Sea level"),
    "tide_ref": ("relativo ao nível médio", "relative to mean sea level"),
    "tide_hw": ("preia-mar", "high tide"),
    "dec_h": ("Resumo para decisão", "Decision summary"),
    "dec_central": ("Cenário central", "Central scenario"),
    "dec_upper": ("Pior cenário plausível", "Plausible worst case"),
    "dec_lower": ("Cenário inferior", "Lower scenario"),
    "dec_line": ("{b} edifícios com água e {r} km de estrada intransitável.", "{b} buildings with water and {r} km of road impassable."),
    "dec_roads": ("Cortes: {roads}.", "Cut: {roads}."),
    "dec_from": ("Primeira inundação: {t}.", "First flooding: {t}."),
    "dec_tide": ("Preia-mar na foz do Sado: {t}. Na preia-mar, o estuário dificulta o escoamento do rio em Alcácer.",
                 "High tide at the Sado mouth: {t}. At high tide the estuary slows the river's drainage at Alcácer."),
    "dec_note_tide": ("Indicativo: modelo não calibrado; a maré prevista na foz do Sado entra no modelo hidrodinâmico como nível do mar no limite do estuário, sem atraso nem amplificação até Alcácer. Siga sempre a Proteção Civil.",
                      "Indicative: uncalibrated model; the forecast sea level at the Sado mouth enters the hydrodynamic model at the estuary edge, with no delay or amplification up to Alcácer. Always follow Civil Protection."),
    "dec_note": ("Indicativo: modelo não calibrado; a maré ainda não entra no modelo hidrodinâmico. Siga sempre a Proteção Civil.",
                 "Indicative: uncalibrated model; the tide is not yet in the hydrodynamic model. Always follow Civil Protection."),
    "imp_h": ("Impactos previstos (cenário selecionado)", "Expected impacts (selected scenario)"),
    "imp_p": ("Edifícios, estradas e equipamentos do OpenStreetMap atingidos pela água simulada, com as horas previstas de chegada, pico e secagem. Indicativo: modelo não calibrado.",
              "Buildings, roads and facilities from OpenStreetMap reached by the simulated water, with the expected arrival, peak and drying times. Indicative: model not calibrated."),
    "imp_bld": ("edifícios com água", "buildings with water"),
    "imp_bld_deep": ("com mais de 0,5 m", "deeper than 0.5 m"),
    "imp_road": ("de estrada inundada", "of road flooded"),
    "imp_road_imp": ("intransitável (> 0,3 m)", "impassable (> 0.3 m)"),
    "imp_cut": ("Estradas e arruamentos cortados", "Roads and streets cut"),
    "imp_fac": ("Equipamentos afetados", "Facilities affected"),
    "imp_from": ("a partir de", "from"),
    "j_imp": ("Impactos", "Impacts"),
    "dec_more": ("Ver detalhes ↓", "Full details ↓"),
    "imp_k_road": ("estrada / arruamento", "road / street"),
    "imp_k_bld": ("edifício", "building"),
    "imp_c_place": ("Local", "Place"),
    "imp_c_depth": ("Profundidade máx.", "Max. depth"),
    "imp_c_wet": ("Começa a ter água", "Water arrives"),
    "imp_c_peak": ("Água mais alta", "Highest water"),
    "imp_c_dry": ("Fica seco", "Dry again"),
    "imp_c_snap": ("Imagem", "Snapshot"),
    "imp_snap": ("Ver a inundação simulada neste local", "Show the simulated flood at this place"),
    "imp_stillwet": ("ainda com água no fim da previsão", "still wet at the end of the forecast"),
    "imp_none": ("Nenhum local identificado com nome neste cenário.", "No named place identified in this scenario."),
    "imp_notiming": ("As horas de chegada, pico e secagem por local aparecem a partir da próxima previsão (esta simulação foi feita antes de serem calculadas).",
                     "Arrival, peak and drying times per place appear from the next forecast (this simulation ran before they were computed)."),
    "imp_m_h": ("Como foram determinados os locais afetados", "How the affected places were determined"),
    "imp_m_src": ("<b>Fonte:</b> edifícios, estradas/arruamentos e equipamentos do OpenStreetMap (© contribuidores OSM, ODbL), extraídos uma vez para a área do modelo. Não é o cadastro oficial: podem faltar elementos ou nomes.",
                  "<b>Source:</b> buildings, roads/streets and facilities from OpenStreetMap (© OSM contributors, ODbL), extracted once for the model area. It is not the official register: features or names may be missing."),
    "imp_m_depth": ("<b>Água:</b> para cada célula de 20 m do modelo FLUXOS usa-se a profundidade máxima simulada nas 72 h do cenário selecionado.",
                    "<b>Water:</b> for each 20 m cell of the FLUXOS model the maximum simulated depth over the 72 h of the selected scenario is used."),
    "imp_m_bld": ("<b>Edifícios:</b> verifica-se a água nos vértices e no centro de cada edifício; conta como «com água» se algum desses pontos tiver ≥ 0,10 m, e «com mais de 0,5 m» se tiver ≥ 0,50 m. Os edifícios não existem no terreno do modelo (a água pode ocupar a célula onde estão), por isso isto indica exposição, não água dentro de casa.",
                  "<b>Buildings:</b> water is checked at each building's corners and centre; it counts as «with water» if any of those points has ≥ 0.10 m, and «deeper than 0.5 m» if ≥ 0.50 m. Buildings are not in the model terrain (water can occupy their cell), so this means exposure, not water inside."),
    "imp_m_road": ("<b>Estradas:</b> pontos a cada 10 m ao longo do eixo de cada estrada; comprimento «inundado» com ≥ 0,10 m e «intransitável» com ≥ 0,30 m (limite da norma australiana ARR 2019 para veículos ligeiros). Uma estrada com nome ou número aparece como «cortada» se algum troço estiver intransitável. Pontes e túneis são excluídos (o terreno do modelo não os representa).",
                   "<b>Roads:</b> points every 10 m along each road's centre line; length «flooded» with ≥ 0.10 m and «impassable» with ≥ 0.30 m (Australian ARR 2019 limit for small vehicles). A named or numbered road is listed as «cut» if any stretch is impassable. Bridges and tunnels are excluded (the model terrain does not represent them)."),
    "imp_m_fac": ("<b>Equipamentos:</b> os do OSM nesta área — escolas e jardins de infância, centros de saúde, farmácias, bombeiros, polícia, câmara municipal, mercados, postos de combustível, locais de culto, subestações elétricas, ETAR e estações elevatórias — com ≥ 0,10 m em algum ponto; indica-se a perigosidade ARR máxima (H1–H6).",
                  "<b>Facilities:</b> those in OSM for this area — schools and kindergartens, clinics, pharmacies, fire and police stations, town hall, markets, fuel stations, places of worship, electrical substations, wastewater and pumping stations — with ≥ 0.10 m at any point; the maximum ARR hazard (H1–H6) is shown."),
    "imp_m_time": ("<b>Horas:</b> o FLUXOS grava a água a cada 15 min. «Começa a ter água» = primeira gravação com ≥ 0,10 m em algum ponto do local; «Água mais alta» = instante da profundidade máxima no ponto mais fundo; «Fica seco» = primeira gravação depois da última com água em todos os pontos do local; se ainda houver água às 72 h, indica-se «ainda com água no fim da previsão». Horas de Lisboa; precisão ±15 min.",
                   "<b>Times:</b> FLUXOS saves the water every 15 min. «Water arrives» = first save with ≥ 0.10 m at any point of the place; «Highest water» = time of the maximum depth at the deepest point; «Dry again» = first save after the last one with water at all points of the place; if there is still water at 72 h, «still wet at the end of the forecast» is shown. Lisbon time; ±15 min."),
    "imp_m_snap": ("<b>Imagem:</b> o botão 📷 abre o mapa de satélite centrado no ponto mais fundo do local, com a profundidade máxima simulada por cima.",
                   "<b>Snapshot:</b> the 📷 button opens the satellite map centred on the deepest point of the place, with the maximum simulated depth on top."),
    "imp_m_lim": ("<b>Limitações:</b> terreno de 20 m sem leito do rio nem diques, modelo hidrológico não calibrado e incerteza da chuva prevista. Os números são indicativos e são recalculados automaticamente a cada nova previsão.",
                  "<b>Limitations:</b> 20 m terrain without river bed or levees, uncalibrated hydrological model and uncertain rainfall forecast. Figures are indicative and recomputed automatically at every new forecast."),
    "ly_depth": ("Profundidade", "Depth"),
    "ly_arrival": ("Chegada da água", "Water arrival"),
    "ly_hazard": ("Perigosidade", "Hazard"),
    "ly_duration": ("Duração", "Duration"),
    "ly_arrival_l": ("Tempo até a água chegar (desde o início da previsão)", "Time until the water arrives (from the forecast start)"),
    "ly_duration_l": ("Tempo inundado", "Time flooded"),
    "ly_hazard_l": ("Perigosidade máxima (ARR 2019)", "Maximum hazard (ARR 2019)"),
    "ly_hazard_note": ("H1 seguro em geral · H2 perigoso p/ veículos pequenos · H3 p/ veículos, crianças e idosos · H4 p/ pessoas e veículos · H5 edifícios vulneráveis · H6 todos os edifícios vulneráveis",
                       "H1 generally safe · H2 unsafe for small vehicles · H3 for vehicles, children, elderly · H4 for people and vehicles · H5 buildings vulnerable · H6 all buildings vulnerable"),
    "t_max_btn": ("Profundidade máxima", "Maximum depth"),
    "t_max": ("máximo", "maximum"),
    "kpi_na": ("não calculado neste ciclo", "not computed this cycle"),
    "kpi_at": ("às", "at"),
    "how_h": ("Como foi feita esta previsão", "How this forecast was made"),
    "step1_t": ("Chuva prevista", "Forecast rainfall"),
    "step1_d": ("ECMWF IFS, 4 vezes por dia", "ECMWF IFS, 4 times a day"),
    "step2_t": ("Caudal no rio", "River flow"),
    "step2_d": ("SUMMA–mizuRoute, conjunto de membros", "SUMMA–mizuRoute ensemble"),
    "step3_t": ("Zonas inundadas", "Flooded areas"),
    "step3_d": ("FLUXOS 2D, só se o caudal o justificar", "FLUXOS 2-D, only if the flow warrants it"),
    "details": ("Detalhes técnicos", "Technical details"),
    "official_btn": ("Avisos oficiais (ANEPC)", "Official warnings (ANEPC)"),
    "foot_about": ("Protótipo de investigação do projeto BUR_PC (INTERREG-POCTEP) desenvolvido na Universidade de Évora (MED e CREATE). Não substitui os avisos oficiais.",
                   "Research prototype of the BUR_PC project (INTERREG-POCTEP), developed at the University of Évora (MED and CREATE). It does not replace official warnings."),
    "foot_links": ("Navegação", "Navigation"),
    "foot_partners": ("Instituições", "Institutions"),
    "nav_history": ("Histórico", "History"),
    "nav_about": ("Método e limitações", "Method and limitations"),
    "lvl_none": ("Sem alerta", "No alert"),
    "lvl_watch": ("Vigilância", "Watch"),
    "lvl_warning": ("Aviso", "Warning"),
    "lvl_alert": ("Alerta", "Alert"),
    "lvl_unknown": ("Sem dados fiáveis", "No reliable data"),
    "lvl_none_d": (
        "Neste momento a previsão não indica cheias no rio Sado em Alcácer do Sal.",
        "The forecast currently does not indicate flooding on the Sado river at Alcácer do Sal.",
    ),
    "lvl_watch_d": (
        "A previsão indica chuva ou caudais acima do normal. Acompanhe a situação.",
        "The forecast indicates above-normal rain or flows. Keep following the situation.",
    ),
    "lvl_warning_d": (
        "A previsão indica um risco significativo de cheia. Siga as indicações da Proteção Civil.",
        "The forecast indicates a significant flood risk. Follow Civil Protection instructions.",
    ),
    "lvl_alert_d": (
        "A previsão indica um risco elevado de cheia. Siga de imediato as indicações da Proteção Civil.",
        "The forecast indicates a high flood risk. Follow Civil Protection instructions immediately.",
    ),
    "lvl_unknown_d": (
        "O sistema não conseguiu produzir uma previsão fiável. Não assuma que não há risco: consulte as fontes oficiais.",
        "The system could not produce a reliable forecast. Do not assume there is no risk: check the official sources.",
    ),
    "updated": ("Última atualização", "Last update"),
    "grp_h": ("Previsão de áreas inundadas", "Flooded area forecast"),
    "strips_h": ("Chuva, caudal e maré nas próximas 72 h", "Rain, river flow and tide over the next 72 h"),
    "strips_p": ("— a linha vertical marca o instante mostrado no mapa e na animação.",
                 "— the vertical line marks the moment shown on the map and in the animation."),
    "tl_h": ("Atualizações da previsão", "Forecast updates"),
    "tl_run": ("Previsão meteorológica usada ({h} UTC)", "Weather forecast used ({h} UTC)"),
    "tl_done": ("Resultados publicados", "Results published"),
    "tl_nextpub": ("Próxima previsão disponível ({h} UTC)", "Next forecast available ({h} UTC)"),
    "tl_nextres": ("Novos resultados", "New results"),
    "fc_last": ("<strong>Previsão de chuva usada:</strong> previsão das {t} (ECMWF IFS + {s} fontes, {n} previsões).",
                "<strong>Rain forecast used:</strong> {t} forecast (ECMWF IFS + {s} sources, {n} forecasts)."),
    "fc_next": ("<strong>Próxima previsão:</strong> previsão das {t}; publicação esperada por volta de {p}, resultados atualizados cerca de {r}.",
                "<strong>Next forecast:</strong> {t} forecast; expected to be published around {p}, updated results around {r}."),
    "cycle": ("Ciclo de previsão (ECMWF)", "Forecast cycle (ECMWF)"),
    "stale": (
        "A previsão está desatualizada (mais de 18 horas). Não a use para decisões; consulte as fontes oficiais.",
        "This forecast is out of date (more than 18 hours old). Do not use it for decisions; check the official sources.",
    ),
    "noscript": (
        "Ative o JavaScript para ver o mapa, a alternância de idioma e o aviso de dados desatualizados.",
        "Enable JavaScript to see the map, the language toggle and the out-of-date warning.",
    ),
    "disc_short": (
        'Emergência: <b>112</b> · Avisos oficiais: <a href="https://www.prociv.gov.pt">ANEPC</a>, <a href="https://www.ipma.pt">IPMA</a>, <a href="https://snirh.apambiente.pt">APA</a>',
        'Emergency: <b>112</b> · Official warnings: <a href="https://www.prociv.gov.pt">ANEPC</a>, <a href="https://www.ipma.pt">IPMA</a>, <a href="https://snirh.apambiente.pt">APA</a>',
    ),
    "run_h": ("Cálculo em curso", "Computation in progress"),
    "run_p": ("Este ciclo está a ser recalculado desde {t}. Os resultados abaixo são do cálculo anterior e serão substituídos quando terminar.",
              "This cycle has been recomputing since {t}. The results below are from the previous computation and will be replaced when it finishes."),
    "err_h": ("O que correu mal neste ciclo", "What went wrong in this cycle"),
    "err_data": ("não foi possível descarregar a previsão meteorológica do ECMWF (servidor indisponível ou sobrecarregado).",
                 "the ECMWF weather forecast could not be downloaded (server unavailable or overloaded)."),
    "err_model": ("o modelo não terminou corretamente.", "the model did not finish correctly."),
    "err_other": ("ocorreu um erro inesperado.", "an unexpected error occurred."),
    "err_retry": ("O sistema volta a tentar automaticamente dentro de minutos. Até lá, não assuma que não há risco.",
                  "The system will try again automatically within minutes. Until then, do not assume there is no risk."),
    "legal_h": ("Aviso legal", "Legal notice"),
    "legal": ('Informação meramente indicativa, produzida para fins de investigação. Não substitui as comunicações e avisos oficiais das autoridades (Proteção Civil/ANEPC, IPMA, APA), que prevalecem sempre. A Universidade de Évora e os parceiros do projeto BUR_PC não assumem qualquer responsabilidade por decisões tomadas, ou por danos resultantes, da utilização desta informação.', 'This information is for guidance only and is produced for research purposes. It does not replace the official communications and warnings of the authorities (Civil Protection/ANEPC, IPMA, APA), which always prevail. The University of Évora and the BUR_PC project partners accept no liability for any decision taken, or any damage arising, from the use of this information.'),
    "disc_title": (
        "Protótipo de investigação, não é um serviço oficial de avisos: informação indicativa que não substitui as comunicações oficiais das autoridades.",
        "Research prototype, not an official warning service: for guidance only, it does not replace official communications from the authorities.",
    ),
    "disc_body": (
        "Desenvolvido no projeto BUR_PC (INTERREG-POCTEP) pela Universidade de Évora e parceiros. Em caso de emergência ligue 112. "
        "Para avisos oficiais consulte a Proteção Civil (ANEPC), o IPMA e a APA.",
        "Developed in the BUR_PC project (INTERREG-POCTEP) by the University of Évora and partners. In an emergency call 112. "
        "For official warnings use Civil Protection (ANEPC), IPMA and APA.",
    ),
    "uncal": (
        "O modelo ainda não está calibrado nem validado: os valores podem estar muito errados.",
        "The model is not yet calibrated or validated: values can be very wrong.",
    ),
    "stages": ("Etapas deste ciclo", "Stages of this cycle"),
    "st_screening": ("Triagem da precipitação", "Rainfall screening"),
    "st_hydrology": ("Hidrologia (SUMMA + mizuRoute)", "Hydrology (SUMMA + mizuRoute)"),
    "st_hydraulics": ("Hidráulica (FLUXOS)", "Hydraulics (FLUXOS)"),
    "s_done": ("concluída", "done"),
    "s_skipped": ("não necessária", "not needed"),
    "s_failed": ("falhou", "failed"),
    "s_unknown": ("sem informação", "no information"),
    "precip_h": ("Previsão de precipitação na bacia", "Rainfall outlook for the basin"),
    "precip_p": (
        "Precipitação média na bacia a montante de Alcácer do Sal (membro de controlo do ECMWF IFS, 0,25°).",
        "Mean rainfall over the basin upstream of Alcácer do Sal (ECMWF IFS control member, 0.25°).",
    ),
    "interval": ("Por intervalo (mm)", "Per interval (mm)"),
    "cumul": ("Acumulada (mm)", "Cumulative (mm)"),
    "win_h": ("Janelas de acumulação", "Accumulation windows"),
    "win_hours": ("Janela", "Window"),
    "win_basin": ("Máx. média na bacia (mm)", "Max basin mean (mm)"),
    "win_hru": ("Máx. numa sub-bacia (mm)", "Max in a sub-basin (mm)"),
    "win_start": ("Início", "Start"),
    "win_limits": ("Limiares", "Thresholds"),
    "hydro_h": ("Previsão do caudal no Sado em Alcácer do Sal", "Sado river flow forecast at Alcácer do Sal"),
    "hydro_p": (
        "Previsão por conjunto: a faixa mostra o intervalo entre os percentis 10 e 90 dos membros; a linha é a mediana.",
        "Ensemble forecast: the band spans the 10th to 90th percentile of members; the line is the median.",
    ),
    "band": ("P10 a P90", "P10 to P90"),
    "median": ("Mediana", "Median"),
    "minmax": ("Mínimo e máximo", "Minimum and maximum"),
    "thr": ("Limiar", "Threshold"),
    "exc_h": ("Probabilidade de exceder os limiares", "Probability of exceeding thresholds"),
    "exc_level": ("Nível", "Level"),
    "exc_thr": ("Caudal limiar (m³/s)", "Threshold flow (m³/s)"),
    "exc_prob": ("Probabilidade", "Probability"),
    "peak": ("Pico previsto", "Forecast peak"),
    "peak_txt": (
        "Mediana {p50} m³/s (hora do pico: {t}); P90 {p90} m³/s; máximo dos membros {mx} m³/s. Membros: {n}.",
        "Median {p50} m³/s (peak time: {t}); P90 {p90} m³/s; ensemble maximum {mx} m³/s. Members: {n}.",
    ),
    "state0": ("Estado inicial do modelo", "Model initial state"),
    "state0_txt": ("válido em {t} (idade {a} h)", "valid at {t} (age {a} h)"),
    "rainmap_h": ("Precipitação prevista, grelha ECMWF IFS 0,25° (72 h)", "Forecast rainfall, ECMWF IFS 0.25° grid (72 h)"),
    "rainmap_scn_h": ("Chuva prevista por cenário, células de 0,25° (72 h)", "Forecast rain by scenario, 0.25° cells (72 h)"),
    "lg_rivers": ("Rede hidrográfica do modelo (troços do mizuRoute)", "Model river network (mizuRoute reaches)"),
    "lg_toggle": ("— ligar/desligar nas caixas do canto inferior esquerdo do mapa.",
                  "— switch on/off with the tick boxes at the bottom left of the map."),
    "lg_basins": ("Limites das sub-bacias do modelo hidrológico (SUMMA–mizuRoute)", "Sub-basin boundaries of the hydrological model (SUMMA–mizuRoute)"),
    "rainmap_unit": ("mm por intervalo de 3 h", "mm per 3-hour interval"),
    "flowmap_h": ("Caudal previsto nos troços do rio (mediana do conjunto)", "Forecast flow along the river reaches (ensemble median)"),
    "flowmap_scn_h": ("Caudal previsto nos troços do rio, por cenário", "Forecast flow along the river reaches, by scenario"),
    "flowmap_unit": ("m³/s", "m³/s"),
    "play": ("Reproduzir / pausar", "Play / pause"),
    "anim_h": ("Animação da simulação hidrodinâmica", "Hydrodynamic simulation animation"),
    "anim_p": ("Simulação hidrodinâmica 2D forçada pelo hidrograma previsto no ponto de interface. Desbloqueie a vista para rodar (arrastar) e aproximar (roda do rato).",
               "2-D hydrodynamic simulation driven by the forecast hydrograph at the interface point. Unlock the view to rotate (drag) and zoom (scroll)."),
    "anim_hyd": ("Caudal de entrada no modelo FLUXOS e chuva prevista sobre Alcácer do Sal", "FLUXOS inflow discharge and forecast rain over Alcácer do Sal"),
    "anim_hyd2": ("Caudal previsto no ponto de interface (72 h)", "Forecast flow at the interface point (72 h)"),
    "anim_rain": ("Chuva prevista na bacia (cenário selecionado, 72 h)", "Forecast basin rain (selected scenario, 72 h)"),
    "out_sim": ("Fora do período simulado pelo FLUXOS", "Outside the period simulated by FLUXOS"),
    "anim_simwin": ("período simulado pelo FLUXOS", "period simulated by FLUXOS"),
    "anim_q": ("Caudal de entrada (m³/s)", "Inflow (m³/s)"),
    "anim_r": ("Chuva (mm/h)", "Rain (mm/h)"),
    "anim_now": ("Instante da animação", "Animation time"),
    "lock_on": ("Vista bloqueada — clique para explorar", "View locked — click to explore"),
    "sun_lbl": ("Condições de luz locais", "Local light conditions"),
    "lock_off": ("Bloquear vista", "Lock view"),
    "anim_open": ("Abrir numa janela própria", "Open in its own window"),
    "anim_open_p": ("(fica sincronizada com esta página)", "(stays in sync with this page)"),
    "cav_dem": ("O terreno vem de um modelo de superfície (Copernicus GLO-30); a batimetria do rio não está resolvida, por isso a inundação está provavelmente sobrestimada.",
                "Terrain is a surface model (Copernicus GLO-30); river bathymetry is not resolved, so flooding is likely overestimated."),
    "cav_hyd": ("O hidrograma vem de um modelo hidrológico não calibrado.", "The hydrograph comes from a hydrological model that is not yet calibrated."),
    "cav_notide": ("Edifícios, diques, barragens e o efeito da maré não estão representados.",
                   "No buildings, levees, dams or tidal backwater are represented."),
    "cav_tide": ("Edifícios, diques e barragens não estão representados; a maré entra como o nível do mar previsto na foz do Sado, aplicado no limite do estuário sem calibração.",
                 "No buildings, levees or dams are represented; the tide enters as the forecast sea level at the Sado mouth, applied at the estuary edge without calibration."),
    "cav_exp": ("Resultado experimental de investigação; não é um mapa oficial de inundação.", "Experimental research output; not an official flood map."),
    "map_h": ("Mapa de profundidade máxima de inundação", "Maximum flood depth map"),
    "map_scn": ("Cenário", "Scenario"),
    "map_none": (
        "Não foi necessária a simulação hidráulica neste ciclo, por isso não há mapa de inundação.",
        "The hydraulic simulation was not needed in this cycle, so there is no flood map.",
    ),
    "map_info": (
        "Profundidade máxima {d} m, área inundada {a} km², caudal de entrada máximo {q} m³/s, {h} h simuladas.",
        "Maximum depth {d} m, flooded area {a} km², peak inflow {q} m³/s, {h} h simulated.",
    ),
    "depth_legend": ("Profundidade máxima (m)", "Maximum depth (m)"),
    "caveats": ("Limitações desta simulação", "Limitations of this simulation"),
    "reasons": ("Razões técnicas", "Technical reasons"),
    "errors": ("Erros registados", "Recorded errors"),
    "no_data_t": ("O sistema está a iniciar", "The system is starting"),
    "no_data_p": (
        "Ainda não existe nenhuma previsão publicada. Esta página será atualizada automaticamente quando o primeiro ciclo terminar.",
        "No forecast has been published yet. This page will update automatically once the first cycle finishes.",
    ),
    "hist_h": ("Histórico de ciclos", "Cycle history"),
    "hist_cycle": ("Ciclo", "Cycle"),
    "hist_level": ("Nível", "Level"),
    "hist_msg": ("Mensagem", "Message"),
    "hist_empty": ("Sem ciclos anteriores.", "No previous cycles."),
    "back_latest": ("Ver a previsão mais recente", "See the latest forecast"),
    "older_cycle": (
        "Está a ver um ciclo anterior ({c}).",
        "You are viewing an earlier cycle ({c}).",
    ),
    "api": ("Dados em formato aberto", "Open data"),
    "api_p": (
        "Os ficheiros JSON e CSV usados nesta página estão em",
        "The JSON and CSV files behind this page are in",
    ),
    "emerg_h": ("Fontes oficiais e emergência", "Official sources and emergency"),
    "emerg_112": ("Emergência: ligue 112.", "Emergency: call 112."),
    "emerg_prociv": (
        "Proteção Civil (ANEPC): avisos e conselhos de autoproteção",
        "Civil Protection (ANEPC): warnings and self-protection advice",
    ),
    "emerg_ipma": ("IPMA: avisos meteorológicos", "IPMA: weather warnings"),
    "emerg_apa": ("APA / SNIRH: dados hidrológicos oficiais", "APA / SNIRH: official hydrological data"),
    "about_h": ("Método e limitações", "Method and limitations"),
    "about_what_h": ("O que é este sítio", "What this site is"),
    "about_what": (
        "É um protótipo experimental, desenvolvido no projeto BUR_PC (INTERREG-POCTEP) pela Universidade de Évora e parceiros, "
        "que testa um sistema operacional de previsão de cheias para o rio Sado em Alcácer do Sal. "
        "<strong>Não é um serviço oficial de avisos</strong> e não substitui a Proteção Civil, o IPMA ou a APA.",
        "It is an experimental prototype developed in the BUR_PC project (INTERREG-POCTEP) by the University of Évora and partners "
        "that tests an operational flood forecasting system for the Sado river at Alcácer do Sal. "
        "<strong>It is not an official warning service</strong> and does not replace Civil Protection, IPMA or APA.",
    ),
    "about_how_h": ("Como funciona", "How it works"),
    "about_how": (
        "O sistema consulta continuamente as previsões meteorológicas do ECMWF. "
        "(1) Triagem: a precipitação prevista para a bacia é comparada com limiares. "
        "(2) Se forem excedidos, corre-se um conjunto de simulações hidrológicas (SUMMA para os processos na bacia e mizuRoute para o escoamento no rio) e obtém-se o caudal previsto em Alcácer do Sal. "
        "(3) Se o caudal previsto exceder limiares, corre-se o modelo hidrodinâmico FLUXOS para estimar a extensão e a profundidade da inundação junto à vila. "
        "Os níveis Vigilância, Aviso e Alerta resultam destes limiares.",
        "The system continuously checks ECMWF weather forecasts. "
        "(1) Screening: forecast rainfall over the basin is compared with thresholds. "
        "(2) If they are exceeded, an ensemble of hydrological simulations is run (SUMMA for basin processes and mizuRoute for river routing) giving the forecast flow at Alcácer do Sal. "
        "(3) If the forecast flow exceeds thresholds, the FLUXOS hydrodynamic model is run to estimate flood extent and depth near the town. "
        "The Watch, Warning and Alert levels follow from these thresholds.",
    ),
    "about_lim_h": ("Limitações importantes", "Important limitations"),
    "about_lim": (
        (
            "O modelo hidrológico pode não estar calibrado nem validado contra observações; os caudais podem estar muito errados.",
            "A previsão meteorológica tem incerteza, sobretudo para chuva intensa e local, e a resolução (0,25°, cerca de 25 km) é grosseira.",
            "O terreno do modelo hidráulico vem de um modelo digital de superfície; a batimetria do rio e as estruturas (pontes, diques) não estão representadas com rigor.",
            "Os mapas mostram profundidades máximas simuladas, não o momento nem o tempo de chegada da água, e não indicam locais seguros.",
            "A maré e a influência do estuário do Sado estão representadas de forma simplificada (nível do mar previsto na foz, sem calibração) ou podem não estar representadas.",
            "Falhas de dados ou de modelos podem levar a previsões em falta ou desatualizadas; neste caso o sítio mostra «Sem dados fiáveis».",
        ),
        (
            "The hydrological model may not be calibrated or validated against observations; flows can be very wrong.",
            "Weather forecasts are uncertain, especially for intense, local rainfall, and the resolution (0.25°, about 25 km) is coarse.",
            "The hydraulic model terrain comes from a digital surface model; river bathymetry and structures (bridges, dikes) are not represented accurately.",
            "Maps show simulated maximum depths, not timing or arrival time of the water, and do not indicate safe places.",
            "The tide and the influence of the Sado estuary are represented in a simplified way (forecast sea level at the mouth, uncalibrated) or may not be represented.",
            "Data or model failures can lead to missing or outdated forecasts; in that case the site shows “No reliable data”.",
        ),
    ),
    "about_data_h": ("Dados e créditos", "Data and credits"),
    "credits": (
        "Projeto BUR_PC (INTERREG-POCTEP), Universidade de Évora e parceiros. "
        "Dados meteorológicos: ECMWF Open Data (IFS), licença CC-BY-4.0, © ECMWF. "
        "Modelos: SUMMA, mizuRoute e FLUXOS. Mapa de base: © contribuidores do OpenStreetMap. "
        "Este sítio não utiliza cookies nem ferramentas de seguimento.",
        "BUR_PC project (INTERREG-POCTEP), University of Évora and partners. "
        "Weather data: ECMWF Open Data (IFS), CC-BY-4.0 licence, © ECMWF. "
        "Models: SUMMA, mizuRoute and FLUXOS. Base map: © OpenStreetMap contributors. "
        "This site uses no cookies and no tracking.",
    ),
}

ICONS = {
    "none": '<svg viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="21" fill="none" stroke="currentColor" stroke-width="4"/><path d="M14 25l7 7 14-15" fill="none" stroke="currentColor" stroke-width="4.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    "watch": '<svg viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="21" fill="none" stroke="currentColor" stroke-width="4"/><circle cx="24" cy="14" r="3" fill="currentColor"/><path d="M24 22v14" stroke="currentColor" stroke-width="4.5" stroke-linecap="round"/></svg>',
    "warning": '<svg viewBox="0 0 48 48" aria-hidden="true"><path d="M24 4L46 42H2z" fill="none" stroke="currentColor" stroke-width="4" stroke-linejoin="round"/><path d="M24 18v12" stroke="currentColor" stroke-width="4.5" stroke-linecap="round"/><circle cx="24" cy="36" r="2.6" fill="currentColor"/></svg>',
    "alert": '<svg viewBox="0 0 48 48" aria-hidden="true"><path d="M15 3h18l12 12v18L33 45H15L3 33V15z" fill="currentColor"/><path d="M24 12v16" stroke="#fff" stroke-width="5" stroke-linecap="round"/><circle cx="24" cy="36" r="3" fill="#fff"/></svg>',
    "unknown": '<svg viewBox="0 0 48 48" aria-hidden="true"><path d="M24 3l21 21-21 21L3 24z" fill="none" stroke="currentColor" stroke-width="4" stroke-linejoin="round"/><path d="M18.5 19.5a5.5 5.5 0 1 1 7.6 5.1c-1.4.7-2.1 1.5-2.1 3" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round"/><circle cx="24" cy="34" r="2.6" fill="currentColor"/></svg>',
}


def _esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


from .site_es import ES, MESSAGES_ES  # noqa: E402

LANGS = ("pt", "es", "en")  # display order of the language toggle


def _texts(key) -> dict:
    pt, en = T[key]
    return {"pt": pt, "en": en, "es": ES.get(key, en)}


class _I18n:
    def __init__(self, languages):
        order = [lang for lang in LANGS if lang in languages]
        if not order or not ({"pt", "en"} & set(order)):
            raise ValueError("languages must include 'pt' and/or 'en'")
        self.langs = tuple(order)

    def raw(self, key, **kw):
        """Multilingual HTML (one span per language) for a trusted template string."""
        return self._spans(_texts(key), kw)

    def _spans(self, values, kw):
        out = []
        for lang in self.langs:
            text = values.get(lang) or values.get("en") or values.get("pt") or ""
            if kw:
                text = text.format(**{k: v if isinstance(v, _Html) else _esc(v) for k, v in kw.items()})
            out.append(f'<span lang="{lang}">{text}</span>')
        return "".join(out)

    def plain(self, key, lang=None, **kw):
        lang = lang or self.langs[0]
        text = _texts(key)[lang]
        return text.format(**{k: _esc(v) for k, v in kw.items()}) if kw else text

    def level_label(self, level):
        return self.raw(f"lvl_{level}")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _fmt_ts(value) -> str:
    ts = _parse_ts(value)
    return ts.strftime("%d-%m-%Y %H:%M UTC") if ts else ""


class _Html(str):
    """Trusted HTML passed as a value into a translated template (not escaped)."""


def _time_tag(value) -> str:
    ts = _parse_ts(value)
    if ts is None:
        return _esc(value or "")
    return f'<time datetime="{_esc(ts.strftime("%Y-%m-%dT%H:%M:%SZ"))}">{_fmt_ts(value)}</time>'


def _num(value, digits=0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "–"
    return f"{number:,.{digits}f}".replace(",", " ") if digits == 0 else f"{number:.{digits}f}"


def _level(value) -> str:
    return value if value in LEVELS else "unknown"


def _badge(i18n: _I18n, level: str) -> str:
    level = _level(level)
    return (
        f'<span class="badge lvl-{level}">{ICONS[level]}{i18n.level_label(level)}</span>'
    )


# ---------------------------------------------------------------- charts


def _nice_max(value: float) -> float:
    if value <= 0:
        return 1.0
    import math

    exp = math.floor(math.log10(value))
    base = 10**exp
    for mult in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if value <= mult * base:
            return mult * base
    return 10 * base


def _ticks(vmax: float, n: int = 4):
    return [vmax * i / n for i in range(n + 1)]


def _time_labels(times, plot_x0, plot_w, n_labels=6):
    count = len(times)
    if count == 0:
        return ""
    step = max(1, (count - 1) // (n_labels - 1)) if count > 1 else 1
    out = []
    for i in range(0, count, step):
        ts = _parse_ts(times[i])
        if ts is None:
            continue
        x = plot_x0 + (plot_w * i / max(1, count - 1))
        last = i + step >= count and i != 0
        anchor = "end" if last else "middle"
        out.append(
            f'<text class="t-lbl" x="{x:.1f}" y="{{y}}" text-anchor="{anchor}">{ts.strftime("%d/%m %Hh")}</text>'
        )
    return "".join(out)


def _cursor_attr(times, xs, y0, y1, series=None) -> str:
    """Time->x mapping so the page can draw a moving time cursor synced with the animated maps.
    ``series`` (optional) = [{"l": {lang: label}, "u": unit, "v": values per time, "c": colour key,
    "d": decimals}] for the hover read-out (app.js)."""
    pts, keep = [], []
    for i, (t, x) in enumerate(zip(times, xs)):
        dt = _parse_ts(t)
        if dt is not None:
            pts.append((int(dt.timestamp()), round(float(x), 1)))
            keep.append(i)
    if len(pts) < 2:
        return ""
    doc = {"t": [p[0] for p in pts], "x": [p[1] for p in pts], "y0": y0, "y1": y1}
    if series:
        out = []
        for sr in series:
            v = list(sr.get("v") or [])
            d = sr.get("d", 1)
            vals = [None if i >= len(v) or v[i] is None else round(float(v[i]), d) for i in keep]
            out.append({"l": sr["l"], "u": sr.get("u", ""), "c": sr.get("c", ""), "v": vals})
        doc["s"] = out
    return f" data-cursor='{_esc(json.dumps(doc, ensure_ascii=False))}'"


def _sl(i18n: "_I18n", key: str) -> dict:
    """Plain-text label of ``key`` in every site language (hover read-out)."""
    return {lang: re.sub(r"\s*\([^)]*\)$", "", i18n.plain(key, lang)) for lang in i18n.langs}


def _scn_series(i18n: "_I18n", vals: dict, unit: str, d: int = 1, keys=("up", "ce", "lo")) -> list:
    names = {"lo": "scn_lower", "ce": "scn_median", "up": "scn_upper"}
    return [{"l": _sl(i18n, names[k]), "u": unit, "v": vals[k], "c": k, "d": d} for k in keys if vals.get(k)]


def _precip_chart(i18n: _I18n, screening: dict, ens: dict | None = None) -> str:
    series = screening.get("areal_mean_precip_mm") or {}
    times = series.get("times_utc") or []
    interval = [float(v) for v in (series.get("interval_mm") or [])]
    cumulative = [float(v) for v in (series.get("cumulative_mm") or [])]
    count = min(len(times), len(interval))
    if count == 0:
        return ""
    times, interval = times[:count], interval[:count]
    cumulative = cumulative[:count] if len(cumulative) >= count else []
    w, h = 440, 286
    x0, x1, y0, y1 = 40, w - 42, 20, h - 40
    pw, ph = x1 - x0, y1 - y0
    ymax = _nice_max(max(interval) * 1.15 if max(interval) > 0 else 1)
    # ensemble cumulative rain (all sources) sampled at the chart's interval-end times
    band = None
    if ens and ens.get("cum_p90") and screening.get("forecast_cycle"):
        c0 = _parse_ts(screening.get("forecast_cycle"))
        def at(arr, t):
            dt = _parse_ts(t)
            k = int(round((dt - c0).total_seconds() / 3600)) - 1 if dt and c0 else -1
            return float(arr[min(max(k, 0), len(arr) - 1)])
        band = {k: [at(ens[k], t) for t in times] for k in ("cum_p10", "cum_p50", "cum_p90")}
        for nm, arr in (ens.get("scenario_cum") or {}).items():
            band["s_" + nm] = [at(arr, t) for t in times]
    cvals = (cumulative or []) + (band["cum_p90"] + band.get("s_upper", []) if band else [])
    cmax = _nice_max(max(cvals) * 1.05) if cvals and max(cvals) > 0 else None
    slot0 = pw / count
    hover = [{"l": _sl(i18n, "interval"), "u": "mm", "v": interval, "c": "bar"}]
    if cumulative:
        hover.append({"l": _sl(i18n, "cumul"), "u": "mm", "v": cumulative, "c": "cum"})
    if band:
        hover += _scn_series(i18n, {"up": band.get("s_upper"), "ce": band.get("s_central"), "lo": band.get("s_lower")}, "mm")
    cursor = _cursor_attr(times, [x0 + slot0 * (i + 0.5) for i in range(count)], y0, y1, hover)
    parts = [
        f'<svg class="chart" viewBox="0 0 {w} {h}"{cursor} role="img" aria-label="{_esc(i18n.plain("precip_h"))} / {_esc(i18n.plain("precip_h", "en"))}">'
    ]
    for tick in _ticks(ymax):
        y = y1 - ph * tick / ymax
        parts.append(f'<line class="gridl" x1="{x0}" x2="{x1}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text x="{x0 - 6}" y="{y + 4:.1f}" text-anchor="end">{tick:g}</text>')
    slot = pw / count
    barw = max(1.5, slot * 0.78)
    for i, value in enumerate(interval):
        bh = ph * value / ymax
        x = x0 + slot * i + (slot - barw) / 2
        parts.append(f'<rect class="bar" x="{x:.1f}" y="{y1 - bh:.1f}" width="{barw:.1f}" height="{bh:.1f}"/>')
    if cmax and band:
        xs = [x0 + slot * (i + 0.5) for i in range(count)]
        up = " ".join(f"{x:.1f},{y1 - ph * v / cmax:.1f}" for x, v in zip(xs, band["cum_p90"]))
        lo = " ".join(f"{x:.1f},{y1 - ph * v / cmax:.1f}" for x, v in reversed(list(zip(xs, band["cum_p10"]))))
        parts.append(f'<polygon class="cumband" points="{up} {lo}"/>')
        for nm, cls in (("s_lower", "scn lo"), ("s_central", "scn ce"), ("s_upper", "scn up")):
            if nm in band:
                pts = " ".join(f"{x:.1f},{y1 - ph * v / cmax:.1f}" for x, v in zip(xs, band[nm]))
                parts.append(f'<polyline class="{cls}" points="{pts}"/>')
    if cmax and cumulative:
        pts = " ".join(
            f"{x0 + slot * (i + 0.5):.1f},{y1 - ph * v / cmax:.1f}" for i, v in enumerate(cumulative)
        )
        parts.append(f'<polyline class="cum" points="{pts}"/>')
        for tick in _ticks(cmax):
            y = y1 - ph * tick / cmax
            parts.append(f'<text x="{x1 + 6}" y="{y + 4:.1f}">{tick:g}</text>')
    parts.append(f'<line class="axis" x1="{x0}" x2="{x1}" y1="{y1}" y2="{y1}"/>')
    labels = _time_labels(times, x0 + slot / 2, pw - slot, 4).replace("{y}", str(y1 + 18))
    parts.append(labels)
    parts.append("</svg>")
    legend = (
        f'<p class="legend"><span><i style="background:var(--bar)"></i>{i18n.raw("interval")}</span>'
        + (f'<span><i style="background:var(--cum)"></i>{i18n.raw("cumul")}</span>' if cmax else "")
        + (f'<span><i style="background:var(--band)"></i>{i18n.raw("ens_band")}</span>'
           f'<span><i class="ln lo"></i>{i18n.raw("scn_lower")}</span><span><i class="ln ce"></i>{i18n.raw("scn_central")}</span>'
           f'<span><i class="ln up"></i>{i18n.raw("scn_upper")}</span>' if band else "")
        + "</p>"
    )
    return "".join(parts) + legend


def _hydro_chart(i18n: _I18n, summary: dict) -> str:
    times = summary.get("times_utc") or []
    q = summary.get("quantiles_m3s") or {}
    p10, p50, p90 = q.get("p10") or [], q.get("p50") or [], q.get("p90") or []
    count = min(len(times), len(p10), len(p50), len(p90))
    if count < 2:
        return ""
    times = times[:count]
    series = {k: [float(v) for v in (q.get(k) or [])][:count] for k in ("p10", "p50", "p90", "min", "max")}
    thresholds = {
        k: float(v) for k, v in (summary.get("thresholds_m3s") or {}).items() if k in ("watch", "warning", "alert")
    }
    data_max = max(series["p90"] + (series["max"] if len(series["max"]) == count else []) + [0.0])
    ymax_raw = max(data_max * 1.1, (min(thresholds.values()) * 1.15) if thresholds else 0, 1.0)
    ymax = _nice_max(ymax_raw)
    w, h = 440, 306
    x0, x1, y0, y1 = 48, w - 12, 20, h - 40
    pw, ph = x1 - x0, y1 - y0

    def px(i):
        return x0 + pw * i / (count - 1)

    def py(v):
        return y1 - ph * min(v, ymax) / ymax

    cursor = _cursor_attr(times, [px(i) for i in range(count)], y0, y1,
                          _scn_series(i18n, {"up": series["p90"], "ce": series["p50"], "lo": series["p10"]}, "m³/s", 0))
    parts = [
        f'<svg class="chart" viewBox="0 0 {w} {h}"{cursor} role="img" aria-label="{_esc(i18n.plain("hydro_h"))} / {_esc(i18n.plain("hydro_h", "en"))}">'
    ]
    for tick in _ticks(ymax):
        y = py(tick)
        parts.append(f'<line class="gridl" x1="{x0}" x2="{x1}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text x="{x0 - 6}" y="{y + 4:.1f}" text-anchor="end">{tick:g}</text>')
    parts.append(f'<text x="12" y="{y0 + ph / 2:.1f}" transform="rotate(-90 12 {y0 + ph / 2:.1f})" text-anchor="middle">m³/s</text>')
    upper = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(series["p90"]))
    lower = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in reversed(list(enumerate(series["p10"]))))
    parts.append(f'<polygon class="band" points="{upper} {lower}"/>')
    for key in ("min", "max"):
        if len(series[key]) == count:
            pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(series[key]))
            parts.append(f'<polyline class="ext" points="{pts}"/>')
    pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(series["p50"]))
    parts.append(f'<polyline class="med" points="{pts}"/>')
    for name, value in sorted(thresholds.items(), key=lambda kv: kv[1]):
        if value <= ymax:
            y = py(value)
            parts.append(f'<line class="thr thr-{name}" x1="{x0}" x2="{x1}" y1="{y:.1f}" y2="{y:.1f}"/>')
            label = i18n.level_label(name).replace("<span ", "<tspan ").replace("</span>", "</tspan>")
            parts.append(
                f'<text class="thr-t" x="{x1 - 4}" y="{y - 4:.1f}" text-anchor="end">{label}<tspan> {value:g}</tspan></text>'
            )
    parts.append(f'<line class="axis" x1="{x0}" x2="{x1}" y1="{y1}" y2="{y1}"/>')
    parts.append(_time_labels(times, x0, pw, 4).replace("{y}", str(y1 + 18)))
    parts.append("</svg>")
    legend = (
        f'<p class="legend"><span><i style="background:var(--band)"></i>{i18n.raw("band")}</span>'
        f'<span><i style="background:var(--median)"></i>{i18n.raw("median")}</span>'
        f'<span><i style="background:transparent;border-top:1px dashed var(--mute);height:0"></i>{i18n.raw("minmax")}</span></p>'
    )
    return "".join(parts) + legend


# ---------------------------------------------------------------- content blocks


def _state_label(i18n: _I18n, state) -> str:
    key = {"done": "s_done", "skipped": "s_skipped", "failed": "s_failed"}.get(state, "s_unknown")
    return i18n.raw(key)


def _banner(i18n: _I18n, latest: dict | None, status: dict | None, *, show_h1=True) -> str:
    source = status or latest or {}
    level = _level(source.get("level") or (latest or {}).get("level"))
    label = (latest or {}).get("level_label") or ""
    message = source.get("message") or (latest or {}).get("message") or ""
    message_pt = source.get("message_pt") or (latest or {}).get("message_pt")
    message_html = (
        i18n._spans({"pt": _esc(message_pt), "en": _esc(message), "es": _esc(MESSAGES_ES.get(level, message))}, {})
        if message_pt else _esc(message)
    )
    cycle = source.get("cycle") or (latest or {}).get("cycle")
    updated = (latest or {}).get("updated_at") or source.get("finished_at")
    meta = []
    if updated:
        meta.append(f"{i18n.raw('updated')}: {_time_tag(updated)}")
    if cycle:
        meta.append(f"{i18n.raw('cycle')}: {_time_tag(cycle)}")
    tag = "h1" if show_h1 else "h2"
    detail = i18n.raw(f"lvl_{level}_d")
    extra = f'<p class="meta"><span class="mono">{_esc(label)}</span></p>' if label and label.lower() not in {
        i18n.plain(f"lvl_{level}", "pt").lower(),
        i18n.plain(f"lvl_{level}", "en").lower(),
    } else ""
    return (
        f'<div class="banner lvl-{level}" role="status" aria-live="polite">{ICONS[level]}<div>'
        f"<{tag}>{i18n.level_label(level)}</{tag}><p>{detail}</p>"
        f'{"<p>" + message_html + "</p>" if message else ""}'
        f'<p class="meta">{" · ".join(meta)}</p></div>'
        f'<div class="b-actions"><a class="btn" href="https://www.prociv.gov.pt" rel="noopener">{i18n.raw("official_btn")} ↗</a>'
        f'<span class="b-112">112</span></div></div>'
    )


# ECMWF open data appears ~6-7 h after the run time (00/06/12/18 UTC); the whole chain then takes
# ~1.5 h on the laptop (rain sources, 3 hydrology runs, 3 FLUXOS runs). Estimates shown on the page.
ECMWF_PUBLISH_DELAY_H = 7.0
CHAIN_TYPICAL_H = 1.5
CYCLE_STEP_H = 6


def _forecast_timing(i18n: _I18n, data: dict) -> str:
    """Time bar: run used -> results published -> NOW -> next run available -> new results.
    Positions and the "x h ago / in x h" labels are drawn live by app.js (static page)."""
    from datetime import timedelta as _td

    status = data.get("status") or {}
    c0 = _parse_ts(status.get("cycle"))
    if c0 is None:
        return ""
    ens = data.get("precip_ens") or {}
    srcs = [k for k, v in (ens.get("sources") or {}).items() if not str(v).startswith("failed")]
    others = max(0, len(srcs) - 1)
    done = _parse_ts(status.get("finished_at"))
    nxt = c0 + _td(hours=CYCLE_STEP_H)
    pub = nxt + _td(hours=ECMWF_PUBLISH_DELAY_H)
    res = pub + _td(hours=CHAIN_TYPICAL_H)
    last = i18n.raw("fc_last", t=_Html(_time_tag(c0.isoformat())), s=others, n=ens.get("members_total") or "?")
    nxt_html = i18n.raw("fc_next", t=_Html(_time_tag(nxt.isoformat())), p=_Html(_time_tag(pub.isoformat())),
                        r=_Html(_time_tag(res.isoformat())))
    events = [("run", c0, "tl_run", {"h": f"{c0:%H}"}), ("done", done, "tl_done", {}),
              ("nextpub", pub, "tl_nextpub", {"h": f"{nxt:%H}"}), ("nextres", res, "tl_nextres", {})]
    legend = []
    for i, (key, t, label, kw) in enumerate([e for e in events if e[1] is not None], start=1):
        legend.append(f'<li class="tl-{key}" data-t="{t.isoformat()}" data-n="{i}"><span class="tl-n">{i}</span>'
                      f'<span><strong>{i18n.raw(label, **kw)}</strong><span class="tl-when"></span></span></li>')
    return (f'<div class="fctiming" aria-label="{_esc(i18n.plain("tl_h"))}">'
            f'<div class="tl-head"><strong>{i18n.raw("tl_h")}</strong></div>'
            f'<div class="tl-bar" aria-hidden="true"><div class="tl-past"></div><div class="tl-now"><span></span></div></div>'
            f'<ol class="tl-legend">{"".join(legend)}</ol>'
            f'<p class="tl-detail">{last}<br>{nxt_html}</p></div>')


def _stages(i18n: _I18n, status: dict) -> str:
    stages = status.get("stages") or {}
    items = []
    for key, label in (("screening", "st_screening"), ("hydrology", "st_hydrology"), ("hydraulics", "st_hydraulics")):
        stage = stages.get(key) or {}
        items.append(f"<li><strong>{i18n.raw(label)}</strong>: {_state_label(i18n, stage.get('state'))}</li>")
    return f'<section class="card"><h2>{i18n.raw("stages")}</h2><ul class="stages">{"".join(items)}</ul>{_errors(i18n, status)}</section>'


_ERR_KEYS = {"IfsUnavailable": "err_data", "ModelRunError": "err_model", "TimeoutExpired": "err_model"}


def _errors(i18n: _I18n, status: dict) -> str:
    """Plain-language explanation of what failed; the raw records go into 'Technical details'."""
    if status.get("_running_since"):  # this cycle is being computed again right now
        return (f'<div class="runbox"><h3>{i18n.raw("run_h")}</h3>'
                f'<p>{i18n.raw("run_p", t=_fmt_ts(status["_running_since"]) or "–")}</p></div>')
    errors = status.get("errors") or []
    if not errors:
        return ""
    stage_key = {"screening": "step1_t", "state": "step2_t", "hydrology": "step2_t", "hydraulics": "step3_t"}
    lines, seen = [], set()
    for e in errors[:10]:
        if not isinstance(e, dict):
            continue
        key = (e.get("stage"), _ERR_KEYS.get(e.get("type"), "err_other"))
        if key in seen:
            continue
        seen.add(key)
        stage = i18n.raw(stage_key.get(e.get("stage"), "step2_t"))
        lines.append(f"<li><strong>{stage}:</strong> {i18n.raw(key[1])}</li>")
    retry = f'<p class="retry">{i18n.raw("err_retry")}</p>' if status.get("retry_pending") else ""
    raw = "".join(f"<li class='mono'>{_esc(e if isinstance(e, str) else json.dumps(e, ensure_ascii=False))}</li>"
                  for e in errors[:10])
    return (f'<div class="errbox"><h3>{i18n.raw("err_h")}</h3><ul>{"".join(lines)}</ul>{retry}'
            f'{_details(i18n, "<ul>" + raw + "</ul>")}</div>')


def _reasons(i18n: _I18n, decision: dict | None) -> str:
    reasons = (decision or {}).get("reasons") or []
    if not reasons:
        return ""
    return (
        f'<p class="muted">{i18n.raw("reasons")}: '
        + ", ".join(f"<code>{_esc(r)}</code>" for r in reasons)
        + "</p>"
    )


def _scn_buttons(i18n: _I18n, cls: str = "", available: set | None = None) -> str:
    """Lower / Median / Upper rainfall-scenario switch (median selected by default). All switches on
    the page are linked: choosing a scenario anywhere switches every map, chart and the 3-D viewer."""
    btn = "".join(
        f'<button type="button" class="seg" data-scn="{k}" aria-pressed="{"true" if k == "central" else "false"}"'
        f'{"" if available is None or k in available else " disabled"}>'
        f'{i18n.raw(lbl)}</button>'
        for k, lbl in (("lower", "scn_lower"), ("central", "scn_median"), ("upper", "scn_upper")))
    return f'<div class="segs scnsw {cls}" role="group" aria-label="{_esc(i18n.plain("scn_col"))}">{btn}</div>'


def _anim_map(i18n: _I18n, kind: str, data_src: str | None, geo_src: str, title_key: str, unit_key: str,
              scenarios: bool = False) -> str:
    """Right-hand animated Leaflet map (rain on the forecast grid or flow per reach)."""
    if not data_src:
        return ""
    return (
        f'<div class="animmap"><h3>{i18n.raw(title_key)}</h3>{_scn_buttons(i18n) if scenarios else ""}'
        f'<div class="amap" id="{kind}-map" data-kind="{kind}" data-src="{_esc(data_src)}" data-geo="{_esc(geo_src)}" '
        f'role="region" aria-label="{_esc(i18n.plain(title_key))}"></div>'
        f'<div class="animctl">'
        f'<input type="range" class="slider" min="0" max="0" value="0" aria-label="time">'
        f'<span class="tlabel"></span></div>'
        f'<div class="ramp" data-unit="{_esc(i18n.plain(unit_key))}"></div>'
        + (f'<p class="maplegend"><span><i class="ln-basin"></i>{i18n.raw("lg_basins")}</span> '
           f'<span><i class="ln-river"></i>{i18n.raw("lg_rivers")}</span> <span class="muted">{i18n.raw("lg_toggle")}</span></p>'
           if kind == "rain" else "")
        + '</div>'
    )


def _split(left: str, right: str) -> str:
    return f'<div class="split"><div>{left}</div>{right}</div>' if right else left


def _sources_block(i18n: _I18n, ens: dict | None, details_out: list | None = None) -> str:
    """Which precipitation products were used, which failed, and the three chosen scenarios."""
    if not ens or not ens.get("sources"):
        return ""
    rows = "".join(
        f"<tr><td>{_esc(name)}</td><td>{'✓ ' + _esc(st) if not str(st).startswith('failed') else '✗ ' + _esc(st)}</td></tr>"
        for name, st in ens["sources"].items())
    chosen = "".join(
        f"<li><strong>{i18n.raw('scn_' + k)}:</strong> {_esc(v.get('source'))} · {_esc(v.get('member'))} — "
        f"{_num(v.get('total_mm'), 1)} mm / {ens.get('window_h', 72)} h</li>"
        for k, v in (ens.get("scenarios") or {}).items())
    if details_out is not None:   # the per-source table goes into the section's single "technical details"
        details_out.append(f'<h3>{i18n.raw("src_tbl_h")}</h3><div class="tablewrap"><table><tbody>{rows}</tbody></table></div>')
    return (f'<h3>{i18n.raw("src_h")}</h3><p class="muted">{i18n.raw("src_p", n=ens.get("members_total", "–"))}</p>'
            f'<ul class="scnlist">{chosen}</ul>')


def _screening_block(i18n: _I18n, screening: dict | None, rain_src: str | None = None, geo: str = "",
                     ens: dict | None = None) -> str:
    if not screening:
        return ""
    scn_src = (rain_src.replace("precip_grid.json", "precip_scenarios.json")
               if rain_src and ens and ens.get("_scn_file") else None)
    src_details: list = []
    chart = _split(_precip_chart(i18n, screening, ens) + _sources_block(i18n, ens, src_details),
                   _anim_map(i18n, "rain", scn_src or rain_src, geo + "hrus.geojson",
                             "rainmap_scn_h" if scn_src else "rainmap_h", "rainmap_unit", scenarios=bool(ens)))
    thresholds = screening.get("thresholds") or {}
    rows = []
    for win in screening.get("windows") or []:
        hours = str(win.get("hours"))
        limits = []
        for level in ("watch", "warning"):
            limit = (thresholds.get(level) or {}).get(hours)
            if limit is not None:
                limits.append(f"{i18n.level_label(level)} ≥ {_num(limit)} mm")
        rows.append(
            "<tr>"
            f"<td>{_esc(hours)} h</td><td>{_num(win.get('max_areal_mean_mm'), 1)}</td>"
            f"<td>{_num(win.get('max_hru_mm'), 1)}</td><td>{_time_tag(win.get('start_utc'))}</td>"
            f"<td>{' · '.join(limits) or '–'}</td></tr>"
        )
    table = (
        f'<h3>{i18n.raw("win_h")}</h3><div class="tablewrap"><table><thead><tr><th>{i18n.raw("win_hours")}</th>'
        f'<th>{i18n.raw("win_basin")}</th><th>{i18n.raw("win_hru")}</th><th>{i18n.raw("win_start")}</th>'
        f'<th>{i18n.raw("win_limits")}</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        if rows
        else ""
    )
    return (
        f'<section class="card" id="precip"><h2><span class="num">1</span>{i18n.raw("precip_h")}</h2><p class="muted">{i18n.raw("precip_p")}</p>'
        f"{chart}{_details(i18n, ''.join(src_details) + table + _reasons(i18n, screening.get('decision')))}</section>"
    )


def _hydrology_block(i18n: _I18n, summary: dict | None, flow_src: str | None = None, geo: str = "") -> str:
    if not summary:
        return ""
    chart = _split(_hydro_chart(i18n, summary),
                   _anim_map(i18n, "flow", flow_src, geo + "rivers.geojson",
                             "flowmap_scn_h" if summary.get("scenario_levels") else "flowmap_h", "flowmap_unit",
                             scenarios=bool(summary.get("scenario_levels"))))
    peak = summary.get("peak") or {}
    peak_txt = i18n.raw(
        "peak_txt",
        p50=_num(peak.get("p50_m3s")),
        p90=_num(peak.get("p90_m3s")),
        mx=_num(peak.get("max_m3s")),
        t=_fmt_ts(peak.get("time_of_p50_peak_utc")) or "–",
        n=summary.get("members", "–"),
    )
    thresholds = summary.get("thresholds_m3s") or {}
    probs = summary.get("exceedance_probability") or {}
    rows = []
    if summary.get("scenario_levels"):
        pk = summary.get("scenario_peaks_m3s") or {}
        srows = "".join(
            f"<tr><td>{i18n.raw('scn_' + n)}</td><td>{_num(pk.get(n))} m³/s</td><td>{_badge(i18n, lv)}</td></tr>"
            for n, lv in summary["scenario_levels"].items())
        peak_txt = i18n.raw("scn_peak_txt", t=_fmt_ts(peak.get("time_of_p50_peak_utc")) or "–")
        table = (f'<h3>{i18n.raw("scn_h")}</h3><div class="tablewrap"><table><thead><tr><th>{i18n.raw("scn_col")}</th>'
                 f'<th>{i18n.raw("peak")}</th><th>{i18n.raw("exc_level")}</th></tr></thead><tbody>{srows}</tbody></table></div>')
        state = summary.get("initial_state") or {}
        state_html = (f'<p class="muted">{i18n.raw("state0")}: '
                      f'{i18n.raw("state0_txt", t=_fmt_ts(state.get("valid_time_utc")), a=_num(state.get("age_hours"), 1))}</p>'
                      if state.get("valid_time_utc") else "")
        return (
            f'<section class="card" id="hydrology"><h2><span class="num">2</span>{i18n.raw("hydro_h")}</h2>'
            f'<p class="muted">{i18n.raw("hydro_p_scn")}</p>'
            f'{chart}<h3>{i18n.raw("peak")}</h3><p>{peak_txt}</p>{table}'
            f'{_details(i18n, state_html + _reasons(i18n, summary.get("decision")))}</section>'
        )
    for level in ("watch", "warning", "alert"):
        if level not in thresholds and level not in probs:
            continue
        p = max(0.0, min(1.0, float(probs.get(level, 0) or 0)))
        rows.append(
            f"<tr><td>{_badge(i18n, level)}</td><td>{_num(thresholds.get(level))}</td>"
            f'<td><div class="prob"><div class="track" aria-hidden="true"><div class="fill" style="width:{p * 100:.0f}%"></div></div>'
            f"<strong>{p * 100:.0f} %</strong></div></td></tr>"
        )
    table = (
        f'<h3>{i18n.raw("exc_h")}</h3><div class="tablewrap"><table><thead><tr><th>{i18n.raw("exc_level")}</th>'
        f'<th>{i18n.raw("exc_thr")}</th><th>{i18n.raw("exc_prob")}</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        if rows
        else ""
    )
    state = summary.get("initial_state") or {}
    state_html = ""
    if state.get("valid_time_utc"):
        state_html = (
            f'<p class="muted">{i18n.raw("state0")}: '
            f'{i18n.raw("state0_txt", t=_fmt_ts(state.get("valid_time_utc")), a=_num(state.get("age_hours"), 1))}</p>'
        )
    return (
        f'<section class="card" id="hydrology"><h2><span class="num">2</span>{i18n.raw("hydro_h")}</h2><p class="muted">{i18n.raw("hydro_p")}</p>'
        f'{chart}<h3>{i18n.raw("peak")}</h3><p>{peak_txt}</p>{table}'
        f'{_details(i18n, state_html + _reasons(i18n, summary.get("decision")))}</section>'
    )


def _legend_html(i18n: _I18n) -> str:
    items = []
    for low, high, colour in DEPTH_CLASSES:
        text = f"{low:g}–{high:g}" if high is not None else f"> {low:g}"
        items.append(f'<span><i style="background:{colour}"></i>{text}</span>')
    return f'<div class="depthlegend" aria-label="{_esc(i18n.plain("depth_legend"))}"><strong>{i18n.raw("depth_legend")}:</strong>{"".join(items)}</div>'


def _layer_legends(i18n: _I18n) -> str:
    from .hydraulics import ARRIVAL_CLASSES, DURATION_CLASSES, HAZARD_CLASSES

    def hexc(rgb):
        return "#%02x%02x%02x" % rgb

    def items(classes, fmt):
        return "".join(f'<span><i style="background:{hexc(c)}"></i>{fmt(lo, hi)}</span>' for lo, hi, c in classes)

    rng = lambda lo, hi: f"{lo:g}–{hi:g} h" if hi is not None else f"> {lo:g} h"  # noqa: E731
    haz = {1: "H1", 2: "H2", 3: "H3", 4: "H4", 5: "H5", 6: "H6"}
    return (
        f'<div class="depthlegend ly ly-arrival" hidden><strong>{i18n.raw("ly_arrival_l")}:</strong>{items(ARRIVAL_CLASSES, rng)}</div>'
        f'<div class="depthlegend ly ly-duration" hidden><strong>{i18n.raw("ly_duration_l")}:</strong>{items(DURATION_CLASSES, rng)}</div>'
        f'<div class="depthlegend ly ly-hazard" hidden><strong>{i18n.raw("ly_hazard_l")}:</strong>'
        f'{items(HAZARD_CLASSES, lambda lo, hi: haz[int(lo)])}<span class="muted">{i18n.raw("ly_hazard_note")}</span></div>'
    )


def _short_t(iso) -> str:
    """Compact time (the page shows it as e.g. 'qui 19/11 14:00' in Lisbon time)."""
    ts = _parse_ts(iso)
    return f'<time class="short" datetime="{ts.strftime("%Y-%m-%dT%H:%M:%SZ")}">{ts.strftime("%d/%m %H:%M")} UTC</time>' if ts else "–"


def _impacts_section(i18n: _I18n, scenarios: list[dict], prefix: str, data_rel: str) -> str:
    """Section 4: exposure per scenario (the page-wide scenario switch picks which one shows), every
    affected place with its depth and timing and a snapshot of the simulated flood around it."""
    byname = {sc.get("scenario"): sc for sc in scenarios if sc.get("impacts")}
    if not byname:
        return ""
    names = {"p10": "lower", "p50": "central", "p90": "upper"}
    blocks, timed = [], False
    for pid, sc in byname.items():
        im = sc["impacts"]
        timed = timed or bool(im.get("has_timing"))
        b = sc.get("bounds_wgs84") or {}
        img = f"{prefix}{data_rel}/hydraulics/{pid}/{sc.get('image', 'max_depth.png')}"
        bounds = json.dumps([[b.get("south"), b.get("west")], [b.get("north"), b.get("east")]]) if b else ""
        items = ([("road", r["road"], i18n.plain(f"imp_k_road"), r) for r in im.get("roads_cut", [])]
                 + [("fac", f.get("name") or f.get("type"), f.get("type") or "", f) for f in im.get("facilities", [])]
                 + [("bld", n["name"], i18n.plain("imp_k_bld"), n) for n in im.get("named_buildings", [])])
        items.sort(key=lambda it: (it[3].get("wet_from_utc") or it[3].get("first_utc") or "9", -float(it[3].get("max_depth_m") or 0)))
        rows = []
        for kind, name, typ, it in items:
            wet = it.get("wet_from_utc") or it.get("first_utc")
            dry = (i18n.raw("imp_stillwet") if it.get("still_wet") else _short_t(it.get("dry_utc"))) if "still_wet" in it else "–"
            snap = (f'<button type="button" class="snapbtn" data-lat="{it["lat"]}" data-lon="{it["lon"]}" '
                    f'data-img="{_esc(img)}" data-bounds="{_esc(bounds)}" data-name="{_esc(name)}" '
                    f'aria-label="{_esc(i18n.plain("imp_snap"))}" title="{_esc(i18n.plain("imp_snap"))}">📷</button>'
                    if it.get("lat") is not None and bounds else "")
            hz = f' <span class="hz">{_esc(it["hazard"])}</span>' if it.get("hazard") else ""
            rows.append(f'<tr class="k-{kind}"><td><strong>{_esc(name)}</strong><br><span class="muted">{_esc(typ)}</span></td>'
                        f'<td class="num">{_num(it.get("max_depth_m"), 2)} m{hz}</td>'
                        f'<td>{_short_t(wet) if wet else "–"}</td><td>{_short_t(it.get("peak_utc")) if it.get("peak_utc") else "–"}</td>'
                        f'<td>{dry}</td><td class="c">{snap}</td></tr>')
        table = (f'<div class="imptable"><table><thead><tr><th>{i18n.raw("imp_c_place")}</th><th>{i18n.raw("imp_c_depth")}</th>'
                 f'<th>{i18n.raw("imp_c_wet")}</th><th>{i18n.raw("imp_c_peak")}</th><th>{i18n.raw("imp_c_dry")}</th>'
                 f'<th>{i18n.raw("imp_c_snap")}</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>') if rows else \
                f'<p class="muted">{i18n.raw("imp_none")}</p>'
        blocks.append(
            f'<div class="imp imp-{names.get(pid, pid)}">'
            f'<div class="impfig"><span><b>{im.get("buildings_flooded", 0)}</b> {i18n.raw("imp_bld")}</span>'
            f'<span><b>{im.get("buildings_deep", 0)}</b> {i18n.raw("imp_bld_deep")}</span>'
            f'<span><b>{_num(im.get("roads_flooded_km"), 1)} km</b> {i18n.raw("imp_road")}</span>'
            f'<span><b>{_num(im.get("roads_impassable_km"), 1)} km</b> {i18n.raw("imp_road_imp")}</span></div>'
            f'{table}</div>')
    note = "" if timed else f'<p class="notice">{i18n.raw("imp_notiming")}</p>'
    method = "<ul>" + "".join(f"<li>{i18n.raw(k)}</li>" for k in
                              ("imp_m_src", "imp_m_depth", "imp_m_bld", "imp_m_road", "imp_m_fac", "imp_m_time", "imp_m_snap", "imp_m_lim")) + "</ul>"
    return (f'<section class="card" id="impacts"><h2><span class="num">4</span>{i18n.raw("imp_h")}</h2>'
            f'<p class="muted">{i18n.raw("imp_p")}</p>{_scn_buttons(i18n, "imp-scn", available={names[k] for k in byname if k in names})}'
            f'{note}{"".join(blocks)}'
            f'{_details(i18n, "<h3>" + i18n.raw("imp_m_h") + "</h3>" + method)}</section>')


def _hydraulics_block(i18n: _I18n, scenarios: list[dict], status: dict, prefix: str, data_rel: str,
                      likelihood: dict | None = None) -> str:
    if not scenarios:
        stage = (status.get("stages") or {}).get("hydraulics") or {}
        if stage.get("state") == "skipped":
            return (f'<section class="card" id="map-card"><h2><span class="num">3.1</span>{i18n.raw("map_h")}</h2>'
                    f'<p class="muted">{i18n.raw("map_none")}</p></section>')
        return ""
    base = f"{prefix}{data_rel}/hydraulics/"
    names = {"p10": "scn_lower", "p50": "scn_central", "p90": "scn_upper"}
    order = [s_ for s_ in ("p10", "p50", "p90") if any(sc.get("scenario") == s_ for sc in scenarios)]
    order += [sc["scenario"] for sc in scenarios if sc.get("scenario") not in order]
    byname = {sc["scenario"]: sc for sc in scenarios}
    cfg = {"scenarios": [], "likelihood": None}
    for sid in order:
        sc = byname[sid]
        b = sc.get("bounds_wgs84") or {}
        try:
            entry = {"id": sid, "label": i18n.plain(names.get(sid, "map_scn")) if sid in names else sid,
                     "west": float(b["west"]), "south": float(b["south"]), "east": float(b["east"]), "north": float(b["north"]),
                     "image": f"{base}{sid}/{sc.get('image', 'max_depth.png')}",
                     "frames": [{"t": fr.get("time_utc"), "image": f"{base}{sid}/{fr['image']}", "km2": fr.get("flooded_km2")}
                                for fr in sc.get("frames") or [] if fr.get("time_utc")],
                     "area": sc.get("flooded_area_km2"), "depth": sc.get("max_depth_m"), "q": sc.get("inflow_peak_m3s"),
                     "layers": {k: f"{base}{sid}/{v}" for k, v in (sc.get("layers") or {}).items()}}
            cfg["scenarios"].append(entry)
        except (KeyError, TypeError, ValueError):
            continue
    if likelihood and likelihood.get("bounds_wgs84"):
        lb = likelihood["bounds_wgs84"]
        cfg["likelihood"] = {"image": f"{base}{likelihood.get('image', 'likelihood.png')}",
                             "west": lb["west"], "south": lb["south"], "east": lb["east"], "north": lb["north"],
                             "areas": likelihood.get("areas_km2") or {}}
    if not cfg["scenarios"]:
        return ""
    have = {e["id"] for e in cfg["scenarios"]}
    buttons = _scn_buttons(i18n, "map-scn", available={k for k, p in (("lower", "p10"), ("central", "p50"), ("upper", "p90")) if p in have})
    if cfg["likelihood"]:
        buttons += f'<button type="button" class="seg lkbtn" aria-pressed="false">{i18n.raw("lk_btn")}</button>'
    if any(e.get("layers") for e in cfg["scenarios"]):
        buttons += ('<div class="segs lyr" role="group">' + "".join(
            f'<button type="button" class="seg" data-layer="{k}" aria-pressed="{"true" if k == "depth" else "false"}">{i18n.raw("ly_" + k)}</button>'
            for k in ("depth", "arrival", "hazard", "duration")) + "</div>")
    first = byname.get("p50") or scenarios[0]
    info = i18n.raw("map_info", d=_num(first.get("max_depth_m"), 2), a=_num(first.get("flooded_area_km2"), 2),
                    q=_num(first.get("inflow_peak_m3s")), h=_num(first.get("simulated_hours")))
    tide_in = any(sc.get("tide_boundary") for sc in scenarios)
    caveats = [f"<li>{i18n.raw(k)}</li>" for k in ("cav_dem", "cav_hyd", "cav_tide" if tide_in else "cav_notide", "cav_exp")]
    fallback = (f'<div id="map-fallback"><img class="fallbackimg" src="{_esc(cfg["scenarios"][0]["image"])}" '
                f'alt="{_esc(i18n.plain("map_h"))}" loading="lazy"></div>')
    lk_legend = (f'<div class="depthlegend lk" hidden><strong>{i18n.raw("lk_btn")}:</strong>'
                 f'<span><i style="background:#08306b"></i>{i18n.raw("lk_vl")}</span>'
                 f'<span><i style="background:#4292c6"></i>{i18n.raw("lk_l")}</span>'
                 f'<span><i style="background:#c6dbef"></i>{i18n.raw("lk_p")}</span>'
                 f'<span class="muted">{i18n.raw("lk_note")}</span></div>') if cfg["likelihood"] else ""
    mapdata = json.dumps(cfg).replace("</", "<\\/")
    return (
        f'<section class="card" id="map-card"><h2><span class="num">3.1</span>{i18n.raw("map_h")}</h2>'
        f'<div class="mapctl">{buttons}'
        f'<span class="muted map-info">{info}</span></div>'
        f'<div id="map" role="region" aria-label="{_esc(i18n.plain("map_h"))}"></div>{fallback}'
        f'<div class="animctl mapanim"><input type="range" class="slider" min="0" max="0" value="0" aria-label="time">'
        f'<span class="tlabel"></span>'
        f'<button type="button" class="maxbtn" aria-pressed="false">{i18n.raw("t_max_btn")}</button></div>'
        f'<script type="application/json" id="map-data">{mapdata}</script>'
        f'<div class="depthlegend dp">{_legend_html(i18n)}</div>{lk_legend}'
        f'{_layer_legends(i18n) if any(e.get("layers") for e in cfg["scenarios"]) else ""}'
        f'{_details(i18n, "<h3>" + i18n.raw("caveats") + "</h3><ul>" + "".join(caveats) + "</ul>")}</section>'
    )


STRIP_W, STRIP_H = 440, 170   # rain / flow / sea-level strips: three side by side on wide screens


def _anim_rain_strip(i18n: _I18n, ens: dict | None, hours: int = 72) -> str:
    """Compact hourly basin-rain strip of the three rainfall scenarios (selected one shown)."""
    from datetime import timedelta

    if not ens or not ens.get("scenario_cum") or not ens.get("cycle"):
        return ""
    c0 = _parse_ts(ens["cycle"])
    hrs = [h for h in (ens.get("hours") or []) if h < hours]
    n = len(hrs)
    if n < 2 or c0 is None:
        return ""
    rain = {}
    for name, cum in ens["scenario_cum"].items():
        cum = [0.0] + [float(v) for v in cum[:n]]
        rain[name] = [max(0.0, cum[i + 1] - cum[i]) for i in range(n)]
    rmax = _nice_max(max(max(v) for v in rain.values()) * 1.15 or 1)
    times = [(c0 + timedelta(hours=float(h) + 1)).isoformat() for h in hrs]   # interval ends
    w, h = STRIP_W, STRIP_H
    x0, x1, y0, y1 = 40, w - 10, 14, h - 26
    pw, ph = x1 - x0, y1 - y0
    slot = pw / n
    parts = [f'<svg class="chart wide strip rainstrip" viewBox="0 0 {w} {h}"'
             + _cursor_attr(times, [x0 + slot * (i + 1) for i in range(n)], y0, y1,
                            _scn_series(i18n, {"up": rain.get("upper"), "ce": rain.get("central"), "lo": rain.get("lower")}, "mm/h"))
             + f' role="img" aria-label="{_esc(i18n.plain("anim_rain"))}">']
    for tick in _ticks(rmax, 2):
        y = y1 - ph * tick / rmax
        parts.append(f'<line class="gridl" x1="{x0}" x2="{x1}" y1="{y:.1f}" y2="{y:.1f}"/>'
                     f'<text x="{x0 - 6}" y="{y + 4:.1f}" text-anchor="end">{tick:g}</text>')
    cls = {"lower": "lo", "central": "ce", "upper": "up"}
    for name, vals in rain.items():
        bars = "".join(f'<rect x="{x0 + slot * i + 0.5:.1f}" y="{y1 - ph * v / rmax:.1f}" width="{max(0.8, slot - 1):.1f}" '
                       f'height="{ph * v / rmax:.1f}"/>' for i, v in enumerate(vals) if v > 0)
        parts.append(f'<g class="rbars {cls.get(name, name)}">{bars}</g>')
    parts.append(f'<line class="axis" x1="{x0}" x2="{x1}" y1="{y1}" y2="{y1}"/>')
    parts.append(_time_labels(times, x0 + slot, pw - slot, 3).replace("{y}", str(y1 + 16)))
    parts.append("</svg>")
    return (f'<div class="animhyd rainhead"><div class="animhyd-head"><strong>{i18n.raw("anim_rain")}</strong>'
            f'<span class="muted">mm/h</span></div>{"".join(parts)}</div>')


def _tide_strip(i18n: _I18n, tide: dict | None) -> str:
    """Compact sea-level strip (tide + surge at the Sado mouth) on the shared clock."""
    if not tide or not tide.get("sea_level_m"):
        return ""
    times = tide["times_utc"]
    vals = [None if v is None else float(v) for v in tide["sea_level_m"]]
    pts_ok = [(i, v) for i, v in enumerate(vals) if v is not None]
    n = len(times)
    if n < 2 or len(pts_ok) < 2:
        return ""
    w, h = STRIP_W, STRIP_H
    x0, x1, y0, y1 = 40, w - 10, 14, h - 26
    pw, ph = x1 - x0, y1 - y0
    lo = min(v for _, v in pts_ok); hi = max(v for _, v in pts_ok)
    lo, hi = (min(lo, -1.0), max(hi, 1.0))

    def px(i):
        return x0 + pw * i / (n - 1)

    def py(v):
        return y1 - ph * (v - lo) / (hi - lo)

    parts = [f'<svg class="chart wide strip tidestrip" viewBox="0 0 {w} {h}"'
             + _cursor_attr(times, [px(i) for i in range(n)], y0, y1,
                            [{"l": _sl(i18n, "tide_lvl"), "u": "m", "v": vals, "c": "tide", "d": 2}])
             + f' role="img" aria-label="{_esc(i18n.plain("tide_h"))}">']
    for tick in (lo, 0.0, hi):
        y = py(tick)
        parts.append(f'<line class="gridl" x1="{x0}" x2="{x1}" y1="{y:.1f}" y2="{y:.1f}"/>'
                     f'<text x="{x0 - 6}" y="{y + 4:.1f}" text-anchor="end">{tick:+.1f}</text>')
    pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in pts_ok)
    parts.append(f'<polyline class="tide" points="{pts}"/>')
    for ht in tide.get("high_tides") or []:
        if ht["time_utc"] in times:
            i = times.index(ht["time_utc"])
            parts.append(f'<circle class="hw" cx="{px(i):.1f}" cy="{py(float(ht["level_m"])):.1f}" r="3"/>')
    parts.append(f'<line class="axis" x1="{x0}" x2="{x1}" y1="{y1}" y2="{y1}"/>')
    parts.append(_time_labels(times, x0, pw, 3).replace("{y}", str(y1 + 16)))
    parts.append("</svg>")
    return (f'<div class="animhyd tidehead"><div class="animhyd-head"><strong>{i18n.raw("tide_h")}</strong>'
            f'<span class="muted">m ({i18n.raw("tide_ref")}) · ● {i18n.raw("tide_hw")}</span></div>{"".join(parts)}</div>')


def _anim_hydrograph(i18n: _I18n, forcing: dict | None, hyd: dict | None = None, hours: int = 72) -> str:
    """Compact strip above the 3-D viewer: the three scenario hydrographs over the whole forecast
    window (72 h), the FLUXOS-simulated span shaded, and a time cursor on the shared page clock."""
    from datetime import timedelta

    q = (hyd or {}).get("quantiles_m3s") or {}
    times = (hyd or {}).get("times_utc") or []
    series = {"lo": q.get("p10"), "ce": q.get("p50"), "up": q.get("p90")}
    if not times or not series["ce"]:
        return ""
    t0 = _parse_ts(times[0])
    keep = [i for i, t in enumerate(times) if _parse_ts(t) and (_parse_ts(t) - t0).total_seconds() <= hours * 3600]
    times = [times[i] for i in keep]
    series = {k: [float(v[i]) for i in keep] for k, v in series.items() if v}
    n = len(times)
    if n < 2:
        return ""
    w, h = STRIP_W, STRIP_H
    x0, x1, y0, y1 = 40, w - 10, 14, h - 26
    pw, ph = x1 - x0, y1 - y0
    qmax = _nice_max(max(max(v) for v in series.values()) * 1.1 or 1)

    def px(i):
        return x0 + pw * i / (n - 1)

    def py(v):
        return y1 - ph * v / qmax

    parts = [f'<svg class="chart wide strip" viewBox="0 0 {w} {h}"'
             + _cursor_attr(times, [px(i) for i in range(n)], y0, y1, _scn_series(i18n, series, "m³/s", 0))
             + f' role="img" aria-label="{_esc(i18n.plain("anim_hyd"))}">']
    # FLUXOS simulated window
    if forcing and forcing.get("sim_start_utc") and forcing.get("inflow"):
        s0 = _parse_ts(forcing["sim_start_utc"])
        s1 = s0 + timedelta(seconds=float(forcing["inflow"][-1]["t_s"]))
        span = (_parse_ts(times[-1]) - t0).total_seconds() or 1
        xa = x0 + pw * max(0.0, (s0 - t0).total_seconds()) / span
        xb = x0 + pw * min(span, (s1 - t0).total_seconds()) / span
        if xb > xa:
            parts.append(f'<rect class="simwin" x="{xa:.1f}" y="{y0}" width="{xb - xa:.1f}" height="{ph}"/>'
                         f'<text class="simwin-t" x="{xa + 4:.1f}" y="{y0 + 11}">FLUXOS</text>')
    for tick in _ticks(qmax, 3):
        y = py(tick)
        parts.append(f'<line class="gridl" x1="{x0}" x2="{x1}" y1="{y:.1f}" y2="{y:.1f}"/>'
                     f'<text x="{x0 - 6}" y="{y + 4:.1f}" text-anchor="end">{tick:g}</text>')
    for k in ("lo", "up", "ce"):
        if k in series:
            pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(series[k]))
            parts.append(f'<polyline class="scn {k}" points="{pts}"/>')
    parts.append(f'<line class="axis" x1="{x0}" x2="{x1}" y1="{y1}" y2="{y1}"/>')
    parts.append(_time_labels(times, x0, pw, 3).replace("{y}", str(y1 + 16)))
    parts.append("</svg>")
    return (
        f'<div class="animhyd"><div class="animhyd-head"><strong>{i18n.raw("anim_hyd2")}</strong>'
        f'<span class="legend"><span><i class="ln lo"></i>{i18n.raw("scn_lower")}</span>'
        f'<span><i class="ln ce"></i>{i18n.raw("scn_median")}</span><span><i class="ln up"></i>{i18n.raw("scn_upper")}</span>'
        f'<span><i class="sw-sim"></i>{i18n.raw("anim_simwin")}</span></span>'
        f'<span class="animnow"><span class="an-val">–</span></span></div>{"".join(parts)}</div>'
    )


def _animation_block(i18n: _I18n, scenarios: list[dict], prefix: str, data_rel: str, hyd: dict | None = None,
                     ens: dict | None = None, tide: dict | None = None, strips_out: list | None = None) -> str:
    byname = {sc.get("scenario"): sc for sc in scenarios if sc.get("webgl")}
    if not byname:
        return ""
    names = {"p10": "lower", "p50": "central", "p90": "upper"}
    first = byname.get("p50") or next(iter(byname.values()))
    views = {names.get(k, k): {"src": f'{prefix}{data_rel}/hydraulics/{k}/{sc["webgl"]}',
                               "forcing": sc.get("_forcing")} for k, sc in byname.items()}
    src = f'{prefix}{data_rel}/hydraulics/{first["scenario"]}/{first["webgl"]}#lang={i18n.langs[0]}'
    switch = _scn_buttons(i18n, "anim-scn", available=set(views))
    data = json.dumps(views).replace("</", "<\\/")
    # rain / flow / sea-level strips on the shared clock; the caller may place them above map + animation
    strips = f'{_anim_rain_strip(i18n, ens)}{_anim_hydrograph(i18n, first.get("_forcing"), hyd)}{_tide_strip(i18n, tide)}'
    if strips_out is not None:
        strips_out.append(strips)
    return (
        f'<section class="card" id="anim-card"><h2><span class="num">3.2</span>{i18n.raw("anim_h")}</h2><p class="muted">{i18n.raw("anim_p")}</p>'
        f'{switch}<script type="application/json" class="anim-views">{data}</script>'
        f'{"" if strips_out is not None else strips}'
        f'<script type="application/json" class="anim-forcing">{json.dumps({"start": (first.get("_forcing") or {}).get("sim_start_utc"), "inflow": (first.get("_forcing") or {}).get("inflow", []), "rain": (first.get("_forcing") or {}).get("rain", [])}).replace("</", "<\\/")}</script>'
        f'<div class="webglwrap locked"><div class="outsim" hidden>{i18n.raw("out_sim")}</div><iframe src="{_esc(src)}" title="{_esc(i18n.plain("anim_h"))}" loading="lazy" '
        f'allowfullscreen></iframe>'
        f'<div class="viewctl"><button type="button" class="viewlock" aria-pressed="true">'
        f'<svg class="lk-closed" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M17 9h-1V7a4 4 0 10-8 0v2H7a2 2 0 00-2 2v8a2 2 0 002 2h10a2 2 0 002-2v-8a2 2 0 00-2-2zm-7-2a2 2 0 114 0v2h-4V7z"/></svg>'
        f'<svg class="lk-open" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M17 9h-7V7a2 2 0 013.9-.6l1.9-.6A4 4 0 008 7v2H7a2 2 0 00-2 2v8a2 2 0 002 2h10a2 2 0 002-2v-8a2 2 0 00-2-2z"/></svg>'
        f'<span class="lk-t-locked">{i18n.raw("lock_on")}</span><span class="lk-t-open">{i18n.raw("lock_off")}</span></button>'
        f'<button type="button" class="suntoggle" aria-pressed="false">'
        f'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4.5" fill="currentColor"/><g stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8"/></g></svg>'
        f'<span>{i18n.raw("sun_lbl")}</span><span class="sw" aria-hidden="true"><i></i></span></button></div>'
        f'</div>'
        f'<p><button type="button" class="popout" data-src="{_esc(src)}">{i18n.raw("anim_open")} ↗</button>'
        f' <span class="muted">{i18n.raw("anim_open_p")}</span></p></section>'
    )


def _details(i18n: _I18n, inner: str) -> str:
    return f'<details class="tech"><summary>{i18n.raw("details")}</summary>{inner}</details>' if inner else ""


def _decision_block(i18n: _I18n, data: dict) -> str:
    """Decision-oriented headline: what floods, where and from when, per scenario, plus high tides."""
    byname = {sc.get("scenario"): sc for sc in data.get("scenarios") or [] if sc.get("impacts")}
    if not byname:
        return ""
    lines = []
    for pid, key in (("p50", "dec_central"), ("p90", "dec_upper"), ("p10", "dec_lower")):
        sc = byname.get(pid)
        if not sc:
            continue
        im = sc["impacts"]
        roads = ", ".join(r["road"] for r in im.get("roads_cut", [])[:3])
        first = _fmt_ts(_first_utc(sc)) if _first_utc(sc) else None
        txt = i18n.raw("dec_line", b=im.get("buildings_flooded", 0), r=_num(im.get("roads_impassable_km"), 1))
        if roads:
            txt += " " + i18n.raw("dec_roads", roads=roads)
        if first:
            txt += " " + i18n.raw("dec_from", t=first)
        scn = {"p50": "central", "p90": "upper", "p10": "lower"}[pid]
        lines.append(f"<li><strong>{i18n.raw(key)}:</strong> {txt} "
                     f'<a class="detbtn" href="#impacts" data-scn="{scn}">{i18n.raw("dec_more")}</a></li>')
    tide_bc = any(sc.get("tide_boundary") for sc in data.get("scenarios") or [])
    tide = data.get("tide") or {}
    hw = ", ".join(_fmt_ts(h["time_utc"]) for h in (tide.get("high_tides") or [])[:4])
    tide_html = f'<p class="muted">{i18n.raw("dec_tide", t=hw)}</p>' if hw else ""
    return (f'<div class="decision"><h2>{i18n.raw("dec_h")}</h2><ul>{"".join(lines)}</ul>{tide_html}'
            f'<p class="muted small">{i18n.raw("dec_note_tide" if tide_bc else "dec_note")}</p></div>')


def _first_utc(sc: dict) -> str | None:
    fh = sc.get("first_flooding_h")
    st = _parse_ts(sc.get("window_start_utc"))
    if fh is None or st is None:
        return None
    from datetime import timedelta
    return (st + timedelta(hours=float(fh))).isoformat()


def _kpis(i18n: _I18n, data: dict) -> str:
    def card(href, label, value, sub="", tone=""):
        return (f'<a class="kpi {tone}" href="{href}"><span class="k-label">{label}</span>'
                f'<span class="k-value">{value}</span><span class="k-sub">{sub}</span></a>')

    na = f'<span class="k-na">{i18n.raw("kpi_na")}</span>'
    cards = []
    scr = data.get("screening") or {}
    w72 = [w for w in scr.get("windows") or [] if str(w.get("hours")) == "72"]
    rain = f'{_num(w72[0].get("max_areal_mean_mm"), 1)} <small>mm</small>' if w72 else na
    ens = data.get("precip_ens") or {}
    rsub = ""
    if ens.get("cum_p10"):
        k = min(int(ens.get("window_h", 72)), len(ens["cum_p10"])) - 1
        rsub = i18n.raw("kpi_range", lo=_num(ens["cum_p10"][k], 1), hi=_num(ens["cum_p90"][k], 1), u="mm")
    cards.append(card("#precip", i18n.raw("kpi_rain"), rain, rsub))
    hyd = data.get("hydrology") or {}
    peak = hyd.get("peak") or {}
    if peak.get("p50_m3s") is not None:
        rng = (i18n.raw("kpi_range", lo=_num(peak.get("p10_m3s")), hi=_num(peak.get("p90_m3s")), u="m³/s") + " · "
               if peak.get("p10_m3s") is not None else "")
        cards.append(card("#hydrology", i18n.raw("kpi_flow"), f'{_num(peak.get("p50_m3s"))} <small>m³/s</small>',
                          f'{rng}{i18n.raw("kpi_at")} {_fmt_ts(peak.get("time_of_p50_peak_utc")) or "–"}'))
        if hyd.get("scenario_levels"):
            up = hyd["scenario_levels"].get("upper", "none")
            tone = {"alert": "t-alert", "warning": "t-alert", "watch": "t-warn"}.get(up, "")
            cards.append(card("#hydrology", i18n.raw("kpi_worst"), i18n.level_label(up),
                              f'{_num(peak.get("p90_m3s"))} m³/s', tone))
        else:
            prob = float((hyd.get("exceedance_probability") or {}).get("warning", 0) or 0)
            tone = "t-alert" if prob >= 0.5 else ("t-warn" if prob >= 0.2 else "")
            cards.append(card("#hydrology", i18n.raw("kpi_prob"), f"{prob * 100:.0f} <small>%</small>", "", tone))
    else:
        cards.append(card("#hydrology", i18n.raw("kpi_flow"), na))
        cards.append(card("#hydrology", i18n.raw("kpi_prob"), na))
    scn = data.get("scenarios") or []
    byname = {sc.get("scenario"): sc for sc in scn}
    central = byname.get("p50") or (scn[0] if scn else None)
    flood = f'{_num(central.get("flooded_area_km2"), 2)} <small>km²</small>' if central else na
    fsub = ""
    if "p10" in byname and "p90" in byname:
        fsub = i18n.raw("kpi_range", lo=_num(byname["p10"].get("flooded_area_km2"), 2),
                        hi=_num(byname["p90"].get("flooded_area_km2"), 2), u="km²")
    elif central:
        fsub = i18n.raw("kpi_depth", d=_num(central.get("max_depth_m"), 1))
    cards.append(card("#map-card", i18n.raw("kpi_flood"), flood, fsub))
    return f'<div class="kpis">{"".join(cards)}</div>'


_GH = ('<svg class="gh" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 '
       '3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13'
       '-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 '
       '0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 '
       '2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 '
       '1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>')


def _stage_times(status: dict, cdir: Path | None) -> dict:
    """When each step finished: recorded in status.json (since 2026-11-19); for older cycles the time
    of the step's output file, never later than the cycle's overall finish."""
    from datetime import datetime as _dt, timezone as _tz

    stages = status.get("stages") or {}
    out = {k: (stages.get(k) or {}).get("finished_at") for k in ("screening", "hydrology", "hydraulics")}
    if cdir is None:
        return out
    end = _parse_ts(status.get("finished_at"))
    files = {"screening": [cdir / "screening.json"], "hydrology": [cdir / "hydrology" / "summary.json"],
             "hydraulics": sorted(cdir.glob("hydraulics/*/summary.json"))}
    for k, fs in files.items():
        if out[k] or (stages.get(k) or {}).get("state") != "done":
            continue
        ts = [f.stat().st_mtime for f in fs if f.is_file()]
        if ts:
            t = _dt.fromtimestamp(max(ts), _tz.utc)
            if end is not None and t > end:
                t = end
            out[k] = t.isoformat()
    return out


def _stepper(i18n: _I18n, status: dict, cdir: Path | None = None) -> str:
    stages = status.get("stages") or {}
    when = _stage_times(status, cdir)
    marks = {"done": "✓", "failed": "!", "skipped": "–"}
    code = {
        1: [("ECMWF Open Data", "https://github.com/ecmwf/ecmwf-opendata")],
        2: [("SUMMA", "https://github.com/CH-Earth/summa/tree/develop"), ("mizuRoute", "https://github.com/ESCOMP/mizuRoute")],
        3: [("FLUXOS", "https://github.com/ue-hydro/FLUXOS_cpp")],
    }
    items = []
    for n, key in enumerate(("screening", "hydrology", "hydraulics"), start=1):
        state = (stages.get(key) or {}).get("state") or "unknown"
        links = " · ".join(f'<a href="{u}">{_GH}{name}</a>' for name, u in code[n])
        items.append(
            f'<li class="step s-{_esc(state)}"><span class="dot" aria-hidden="true">{marks.get(state, "?")}</span>'
            f'<span class="st"><strong>{n}. {i18n.raw(f"step{n}_t")}</strong>'
            f'<span>{i18n.raw(f"step{n}_d")}</span><em>{_state_label(i18n, state)}'
            f'{(" · " + _time_tag(when[key])) if when.get(key) else ""}</em>'
            f'<span class="code">{links}</span></span></li>'
        )
    return (f'<section class="card how" aria-labelledby="how-h"><h2 id="how-h">{i18n.raw("how_h")}</h2>'
            f'<ol class="stepper">{"".join(items)}</ol>{_errors(i18n, status)}</section>')


def _speed_block(i18n: _I18n) -> str:
    """Animation speed for every chart, map and the 3-D view (one shared clock), with what it does."""
    speeds = [("0", "&#10074;&#10074;", "speed_pause"), ("0.5", "&frac12;&times;", ""), ("1", "1&times;", ""),
              ("2", "2&times;", ""), ("4", "4&times;", "")]
    sp = "".join(f'<button type="button" data-speed="{v}" aria-pressed="{"true" if v == "1" else "false"}"'
                 + (f' title="{_esc(i18n.plain(t))}"' if t else "") + f'>{lab}</button>' for v, lab, t in speeds)
    return (f'<div class="fc-speed"><div class="fc-speed-btns" role="group" aria-label="{_esc(i18n.plain("speed_l"))}">'
            f'<strong>{i18n.raw("speed_l")}</strong>{sp}</div><p>{i18n.raw("speed_p")}</p></div>')


def _jumpbar(i18n: _I18n, data: dict) -> str:
    links = [("summary", "j_summary"), ("precip", "j_rain")]
    if data.get("hydrology"):
        links.append(("hydrology", "j_flow"))
    if data.get("scenarios"):   # section 3 groups the flood map (3.1) and the 3-D animation (3.2)
        has_anim = any(sc.get("webgl") for sc in data["scenarios"])
        links.append(("flood-group" if has_anim else "map-card", "j_flood"))
        if any(sc.get("impacts") for sc in data["scenarios"]):
            links.append(("impacts", "j_imp"))
    links += [("official", "j_official"), ("opendata", "j_data")]
    items = "".join(f'<a href="#{a}" data-target="{a}">{i18n.raw(k)}</a>' for a, k in links)
    return f'<nav class="jumpbar" aria-label="{_esc(i18n.plain("jump"))}"><div class="wrap">{items}</div></nav>'


def _uncal_block(i18n: _I18n, latest: dict | None) -> str:
    system = (latest or {}).get("system") or {}
    if system.get("uncalibrated", True) is False:
        return ""
    return f'<div class="notice" role="note"><strong>{i18n.raw("uncal")}</strong></div>'


def _emergency_block(i18n: _I18n) -> str:
    return (
        f'<section class="card" id="official"><h2>{i18n.raw("emerg_h")}</h2><ul class="emerg">'
        f'<li><strong>{i18n.raw("emerg_112")}</strong></li>'
        f'<li><a href="https://www.prociv.gov.pt">{i18n.raw("emerg_prociv")}</a></li>'
        f'<li><a href="https://www.ipma.pt">{i18n.raw("emerg_ipma")}</a></li>'
        f'<li><a href="https://snirh.apambiente.pt">{i18n.raw("emerg_apa")}</a></li></ul></section>'
    )


# ---------------------------------------------------------------- cycle data


def _csv_has_rows(path: Path) -> bool:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return sum(1 for _ in csv.reader(handle)) > 1
    except OSError:
        return False


def _with(d: dict | None, **extra) -> dict | None:
    return {**d, **extra} if d else d


def _load_cycle(op: Path, rel: str) -> dict:
    """Load everything we know about one cycle directory (``cycles/<id>``)."""
    cdir = op / rel
    hyd_dir = cdir / "hydraulics"
    scenarios = []
    if hyd_dir.is_dir():
        for sub in sorted(p for p in hyd_dir.iterdir() if p.is_dir()):
            summary = _read_json(sub / "summary.json")
            if summary:
                summary.setdefault("scenario", sub.name)
                summary["_forcing"] = _read_json(sub / "forcing.json")
                scenarios.append(summary)
    return {
        "health": _read_json(op / "health.json"),
        "tide": _read_json(cdir / "tide.json"),
        "_cdir": cdir,
        "precip_ens": _with(_read_json(cdir / "hydrology" / "precip_ensemble.json"),
                            _scn_file=(cdir / "precip_scenarios.json").is_file()),
        "likelihood": _read_json(cdir / "hydraulics" / "likelihood.json"),
        "rain_file": (cdir / "precip_grid.json").is_file(),
        "flow_file": (cdir / "hydrology" / "reach_flow.json").is_file(),
        "status": _read_json(cdir / "status.json") or {},
        "screening": _read_json(cdir / "screening.json"),
        "hydrology": _read_json(cdir / "hydrology" / "summary.json"),
        "scenarios": scenarios,
    }


def _cycle_content(i18n: _I18n, latest, data: dict, prefix: str, data_rel: str, *, is_latest: bool, cycle_id: str) -> str:
    status = data["status"]
    health = data.get("health") or {}
    if health.get("state") == "processing" and health.get("cycle") == status.get("cycle"):
        status = {**status, "_running_since": health.get("time")}
    out = [_jumpbar(i18n, data), '<div class="wrap">']
    c0 = _parse_ts(status.get("cycle"))
    if c0 is not None:  # one common animation window for every chart, map and the 3-D viewer
        from datetime import timedelta as _td
        out.append(f'<div id="clockwin" hidden data-start="{c0.isoformat()}" '
                   f'data-end="{(c0 + _td(hours=72)).isoformat()}"></div>')
    if not is_latest:
        out.append(
            f'<div class="notice"><strong>{i18n.raw("older_cycle", c=cycle_id)}</strong>'
            f'<a href="{prefix}index.html">{i18n.raw("back_latest")}</a></div>'
        )
    out.append('<section id="summary" class="hero">')
    out.append(_banner(i18n, latest if is_latest else None, status))
    if is_latest:
        out.append(_forecast_timing(i18n, data))
    out.append(f'<div id="progress" class="progress" data-src="{prefix}api/progress.json" hidden></div>')
    out.append(_decision_block(i18n, data))
    out.append(_kpis(i18n, data))
    out.append(_uncal_block(i18n, latest))
    out.append("</section>")
    out.append(_stepper(i18n, status, data.get("_cdir")))
    out.append(_speed_block(i18n))
    base = f"{prefix}{data_rel}/"
    geo = f"{prefix}geometry/"
    out.append(_screening_block(i18n, data["screening"],
                                base + "precip_grid.json" if data.get("rain_file") else None, geo,
                                data.get("precip_ens")))
    out.append(_hydrology_block(i18n, data["hydrology"],
                                base + "hydrology/reach_flow.json" if data.get("flow_file") else None, geo))
    # flood map (3) and 3-D animation (4) side by side on wide screens (same clock and scenario)
    strips: list = []
    hyd_html = _hydraulics_block(i18n, data["scenarios"], status, prefix, data_rel, data.get("likelihood"))
    anim_html = _animation_block(i18n, data["scenarios"], prefix, data_rel, data.get("hydrology"),
                                 data.get("precip_ens"), data.get("tide"), strips_out=strips)
    strips_html = (f'<section class="card viz-strips" id="flood-group"><h2><span class="num">3</span>{i18n.raw("grp_h")}</h2>'
                   f'<p class="muted"><strong>{i18n.raw("strips_h")}</strong> {i18n.raw("strips_p")}</p>'
                   f'<div class="strip3">{strips[0]}</div></section>') if strips and anim_html else ""
    out.append(f'<div class="viz-outer"><div class="viz-pair">{strips_html}{hyd_html}{anim_html}</div></div>')
    out.append(_impacts_section(i18n, data["scenarios"], prefix, data_rel))
    out.append(_emergency_block(i18n))
    api = f'{prefix}api/{data_rel}/'
    out.append(
        f'<section class="card" id="opendata"><h2>{i18n.raw("api")}</h2><p>{i18n.raw("api_p")} '
        f'<a href="{_esc(prefix)}api/latest.json"><code>api/latest.json</code></a>, '
        f'<a href="{_esc(api)}status.json"><code>{_esc(api)}status.json</code></a>.</p></section>'
    )
    out.append("</div>")
    return "".join(out)


# ---------------------------------------------------------------- pages


def _nav(i18n: _I18n, prefix: str, current: str) -> str:
    items = (
        ("index", "index.html", "nav_forecast"),
        ("history", "history.html", "nav_history"),
        ("about", "about.html", "nav_about"),
    )
    return "".join(
        f'<a href="{prefix}{href}"{" aria-current=\"page\"" if key == current else ""}>{i18n.raw(label)}</a>'
        for key, href, label in items
    )


def _toggle(i18n: _I18n) -> str:
    if len(i18n.langs) < 2:
        return ""
    labels = {"pt": "PT", "en": "EN", "es": "ES"}
    names = {"pt": "Português", "en": "English", "es": "Español"}
    buttons = "".join(
        f'<button type="button" data-set="{lang}" aria-label="{names[lang]}" '
        f'aria-pressed="{"true" if lang == i18n.langs[0] else "false"}">{labels[lang]}</button>'
        for lang in i18n.langs
    )
    return f'<div class="langtoggle" role="group" aria-label="Language / Idioma">{buttons}</div>'


def _burpc_logo(prefix: str) -> str:
    """BUR_PC logo if assets/logos/burpc.(svg|png) is present, else a text badge."""
    # white version on the dark header/footer, like the other institutional logos
    for name in ("burpc_white.svg", "burpc_white.png", "burpc.svg", "burpc.png"):
        if (ASSET_DIR / "logos" / name).is_file():
            return (f'<img class="lg-burpc" src="{prefix}assets/logos/{name}" alt="BUR_PC" '
                    f'title="BUR_PC – INTERREG España–Portugal (POCTEP)">')
    return '<span class="burpc-badge" title="INTERREG VI-A España–Portugal (POCTEP)">BUR_PC</span>'


def _footer(i18n: _I18n, prefix: str = "") -> str:
    return (
        f'<div class="fgrid"><div><p class="f-brand">Floodcast · BUR_PC</p><p>{i18n.raw("foot_about")}</p></div>'
        f'<div><p class="f-h">{i18n.raw("foot_links")}</p><ul class="f-links">'
        f'<li><a href="{prefix}index.html">{i18n.raw("nav_forecast")}</a></li>'
        f'<li><a href="{prefix}history.html">{i18n.raw("nav_history")}</a></li>'
        f'<li><a href="{prefix}about.html">{i18n.raw("nav_about")}</a></li>'
        f'<li><a href="{prefix}api/latest.json">{i18n.raw("api")}</a></li>'
        f'<li><a href="https://github.com/ue-hydro/bur_pc_floodcast">{_GH} bur_pc_floodcast</a></li></ul></div>'
        f'<div><p class="f-h">{i18n.raw("foot_partners")}</p><div class="f-logos">'
        f'{_burpc_logo(prefix)}'
        f'<a href="https://www.uevora.pt" rel="noopener"><img src="{prefix}assets/logos/uevora_white.png" alt="Universidade de Évora"></a>'
        f'<a href="https://www.med.uevora.pt" rel="noopener"><img src="{prefix}assets/logos/med_white.svg" alt="MED"></a>'
        f'<a href="https://www.create.uevora.pt" rel="noopener"><img class="lg-create" src="{prefix}assets/logos/create_white.png" alt="CREATE"></a>'
        f'</div></div></div>'
        f'<div class="f-funding"><img src="{prefix}assets/logos/burpc_funding_banner.jpg" '
        f'alt="Interreg España–Portugal · Cofinanciado por la Unión Europea / pela União Europeia · BUR_PC"></div>'
        f'<p class="f-legal"><strong>{i18n.raw("legal_h")}.</strong> {i18n.raw("legal")}</p>'
        f'<p class="f-credits">{i18n.raw("credits")}</p>'
    )


def _asset_version() -> str:
    """Content hash of the CSS/JS so browsers fetch new assets after an update."""
    import hashlib

    h = hashlib.sha1()
    for name in ("style.css", "app.js"):
        h.update((ASSET_DIR / name).read_bytes())
    return h.hexdigest()[:10]


def _page(
    template: str,
    i18n: _I18n,
    *,
    site_title: str,
    title: str,
    description: str,
    prefix: str,
    current: str,
    content: str,
    updated: str = "",
    base_url: str = "",
    rel_path: str = "",
) -> str:
    canonical = f'<link rel="canonical" href="{_esc(base_url.rstrip("/") + "/" + rel_path)}">' if base_url else ""
    values = {
        "default_lang": i18n.langs[0],
        "langs": ",".join(i18n.langs),
        "title": _esc(title),
        "description": _esc(description),
        "canonical": canonical,
        "prefix": prefix,
        "updated": _esc(updated),
        "site_title": _esc(site_title),
        "nav": _nav(i18n, prefix, current),
        "toggle": _toggle(i18n),
        "disclaimer": f'<span class="d-icon" aria-hidden="true">⚠</span><strong>{i18n.raw("disc_title")}</strong> <span class="d-links">{i18n.raw("disc_short")}</span>',
        "stale_text": i18n.raw("stale"),
        "noscript_text": i18n.raw("noscript"),
        "content": content,
        "footer": _footer(i18n, prefix),
        "tagline": i18n.raw("tagline"),
    }
    if not content.startswith('<nav class="jumpbar"'):
        content = f'<div class="wrap page">{content}</div>'
        values["content"] = content
    values["brand_name"] = _esc(site_title.split(" — ")[0].split(" – ")[0])
    values["burpc_logo"] = _burpc_logo(prefix)
    out = template
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", value)
    out = out.replace("{{assetver}}", _asset_version())
    # external links open in a new tab so the forecast stays open
    out = re.sub(
        r'<a\b[^>]*>',
        lambda m: (m.group(0)[:-1] + ' target="_blank"' + ("" if "rel=" in m.group(0) else ' rel="noopener"') + ">")
        if re.search(r'href="https?://', m.group(0)) and "target=" not in m.group(0) else m.group(0),
        out,
    )
    return out


def _starting_content(i18n: _I18n) -> str:
    return (
        f'<div class="banner lvl-unknown" role="status">{ICONS["unknown"]}<div><h1>{i18n.raw("no_data_t")}</h1>'
        f'<p>{i18n.raw("no_data_p")}</p></div></div>{_emergency_block(i18n)}'
    )


def _about_content(i18n: _I18n) -> str:
    lim = _texts("about_lim")
    items = ""
    for pt, en, es in zip(lim["pt"], lim["en"], lim["es"], strict=True):
        spans = "".join(f'<span lang="{lang}">{_esc(text)}</span>'
                        for lang, text in (("pt", pt), ("en", en), ("es", es)) if lang in i18n.langs)
        items += f"<li>{spans}</li>"
    return (
        f'<h1>{i18n.raw("about_h")}</h1>'
        f'<section class="card"><h2>{i18n.raw("about_what_h")}</h2><p>{i18n.raw("about_what")}</p></section>'
        f'<section class="card"><h2>{i18n.raw("about_how_h")}</h2><p>{i18n.raw("about_how")}</p></section>'
        f'<section class="card"><h2>{i18n.raw("about_lim_h")}</h2><ul>{items}</ul></section>'
        f"{_emergency_block(i18n)}"
        f'<section class="card" id="legal"><h2>{i18n.raw("legal_h")}</h2><p>{i18n.raw("legal")}</p></section>'
        f'<section class="card"><h2>{i18n.raw("about_data_h")}</h2><p>{i18n.raw("credits")}</p></section>'
    )


def _history_entries(op: Path) -> list[dict]:
    history = _read_json(op / "history.json")
    entries = []
    if isinstance(history, dict):
        history = history.get("cycles") or history.get("history")
    if isinstance(history, list):
        for item in history:
            if isinstance(item, dict) and item.get("cycle"):
                entries.append(item)
    if not entries:
        cycles = op / "cycles"
        if cycles.is_dir():
            for cdir in sorted((p for p in cycles.iterdir() if p.is_dir()), reverse=True):
                status = _read_json(cdir / "status.json")
                if status:
                    entries.append(
                        {"cycle": status.get("cycle"), "level": status.get("level"), "message": status.get("message")}
                    )
    return entries


def _cycle_id(cycle_iso) -> str | None:
    ts = _parse_ts(cycle_iso)
    return ts.strftime("%Y%m%d%H") if ts else None


def _history_content(i18n: _I18n, entries: list[dict], pages: set[str]) -> str:
    rows = []
    for item in entries:
        cid = _cycle_id(item.get("cycle"))
        cell = _time_tag(item.get("cycle"))
        if cid in pages:
            cell = f'<a href="cycles/{cid}/index.html">{cell}</a>'
        rows.append(
            f"<tr><td>{cell}</td><td>{_badge(i18n, item.get('level'))}</td><td>{_esc(item.get('message') or '')}</td></tr>"
        )
    body = (
        f'<div class="tablewrap"><table><thead><tr><th>{i18n.raw("hist_cycle")}</th><th>{i18n.raw("hist_level")}</th>'
        f'<th>{i18n.raw("hist_msg")}</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        if rows
        else f'<p class="muted">{i18n.raw("hist_empty")}</p>'
    )
    return f'<h1>{i18n.raw("hist_h")}</h1><section class="card">{body}</section>'


# ---------------------------------------------------------------- public API


def _daily(times, values, how="sum", nd=1):
    """Group a series by Lisbon calendar day -> [{date, value}]."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Lisbon")
    days: dict[str, list[float]] = {}
    for t, v in zip(times or [], values or []):
        dt = _parse_ts(t)
        if dt is None or v is None:
            continue
        days.setdefault(dt.astimezone(tz).strftime("%Y-%m-%d (%a)"), []).append(float(v))
    out = []
    for d, vals in days.items():
        val = sum(vals) if how == "sum" else max(vals)
        out.append({"date_lisbon": d, ("rain_mm" if how == "sum" else "max_median_flow_m3s"): round(val, nd)})
    return out


def _chat_context(latest: dict | None, data: dict, history: list[dict]) -> dict:
    """Compact, self-describing facts about the latest forecast for the Q&A chat (no secrets)."""
    status = data.get("status") or {}
    scr = data.get("screening") or {}
    hyd = data.get("hydrology") or {}
    ctx: dict = {
        "site": "Floodcast Alcácer do Sal - experimental flood forecast for the Sado river at Alcácer do Sal, Portugal "
                "(BUR_PC project, University of Évora: MED and CREATE). Research prototype, NOT an official warning service.",
        "official_channels": {"emergency": "112", "civil_protection": "ANEPC https://www.prociv.gov.pt",
                              "weather_warnings": "IPMA https://www.ipma.pt", "hydrology": "APA/SNIRH https://snirh.apambiente.pt"},
        "legal_notice": "Guidance only, for research purposes; does not replace official communications and warnings of the "
                        "authorities (ANEPC, IPMA, APA), which always prevail. The University of Évora and the BUR_PC partners "
                        "accept no liability for decisions taken or damage arising from the use of this information.",
        "times_are": "UTC unless marked Lisbon",
        "forecast_cycle_utc": (latest or {}).get("cycle") or status.get("cycle"),
        "updated_utc": (latest or {}).get("updated_at") or status.get("finished_at"),
        "alert_level": status.get("level") or (latest or {}).get("level"),
        "alert_levels_meaning": {"none": "no significant flood signal", "watch": "possible rise of the Sado flow",
                                 "warning": "high flows forecast", "alert": "very high flows forecast",
                                 "unknown": "forecast could not be evaluated; do not assume there is no risk"},
        "message_pt": status.get("message_pt"), "message_en": status.get("message"),
        "model_calibrated": not ((latest or {}).get("system") or {}).get("uncalibrated", True),
        "workflow": "1) ECMWF IFS rainfall forecast screened every 6 h; 2) if rain exceeds thresholds, coupled "
                    "SUMMA-mizuRoute hydrological ensemble (11 members) gives river flow at the FLUXOS inflow point just "
                    "upstream of Alcácer do Sal; 3) if flow exceeds thresholds, FLUXOS 2-D hydraulic model maps flooding.",
        "stages": {k: (v or {}).get("state") for k, v in (status.get("stages") or {}).items()},
    }
    if scr:
        ser = scr.get("areal_mean_precip_mm") or {}
        ctx["rainfall"] = {
            "product": scr.get("product"),
            "basin_mean_daily": _daily(ser.get("times_utc"), ser.get("interval_mm")),
            "max_accumulations": [{k: w.get(k) for k in ("hours", "max_areal_mean_mm", "max_hru_mm", "start_utc")}
                                  for w in scr.get("windows") or []],
            "thresholds_mm": scr.get("thresholds"),
        }
    if hyd:
        q = hyd.get("quantiles_m3s") or {}
        ctx["river_flow"] = {
            "location": "Sado river, FLUXOS inflow point just upstream of Alcácer do Sal (basin ~6056 km2)",
            "ensemble_members": hyd.get("members"),
            "peak": hyd.get("peak"),
            "daily_max_of_median": _daily(hyd.get("times_utc"), q.get("p50"), how="max", nd=0),
            "thresholds_m3s": hyd.get("thresholds_m3s"),
            "thresholds_are_placeholders": hyd.get("thresholds_placeholder", True),
            "probability_of_exceeding": hyd.get("exceedance_probability"),
        }
    scn = data.get("scenarios") or []
    if scn:
        s0 = scn[0]
        ctx["flooding"] = {k: s0.get(k) for k in ("scenario", "inflow_peak_m3s", "max_depth_m", "flooded_area_km2",
                                                     "simulated_hours", "window_start_utc", "window_end_utc", "caveats")}
        ctx["flooding"]["area"] = "6 x 6 km window around Alcácer do Sal"
    imp = {sc.get("scenario"): sc.get("impacts") for sc in scn if sc.get("impacts")}
    if imp:
        ctx["impacts_by_scenario"] = {
            {"p10": "lower", "p50": "central", "p90": "upper"}.get(k, k):
            {kk: v.get(kk) for kk in ("buildings_flooded", "buildings_deep", "roads_flooded_km", "roads_impassable_km",
                                      "roads_cut", "facilities")} for k, v in imp.items()}
    td = data.get("tide") or {}
    if td.get("high_tides"):
        ctx["high_tides_sado_mouth"] = td["high_tides"][:6]
    ctx["recent_cycles"] = [{k: h.get(k) for k in ("cycle", "level")} for h in (history or [])[:8]]
    return ctx


def _copy_api(op: Path, api: Path, cycle_ids: list[str]) -> None:
    api.mkdir(parents=True, exist_ok=True)
    for name in ("latest.json", "history.json"):
        if (op / name).is_file():
            shutil.copy2(op / name, api / name)
    for cid in cycle_ids:
        src = op / "cycles" / cid
        for path in src.rglob("*"):
            if path.is_file() and path.suffix.lower() in COPY_SUFFIXES:
                target = api / "cycles" / cid / path.relative_to(src)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)


def build_site(
    operational_dir: Path,
    out_dir: Path,
    *,
    site_title: str = "Floodcast",
    languages=("pt", "es", "en"),
    base_url: str = "",
    chat_endpoint: str = "",
) -> list[Path]:
    """Render the static site into ``out_dir`` and return the files written.

    The site is built in a sibling temporary directory and swapped into place,
    so a half-written site is never served. (The swap is two renames; a request
    landing between them could briefly see a missing directory, which is
    unavoidable for directory-level swaps on a plain filesystem.)
    """
    op = Path(operational_dir)
    out_dir = Path(out_dir)
    i18n = _I18n(languages)
    template = (ASSET_DIR / "page.html").read_text(encoding="utf-8")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:8]
    tmp = out_dir.with_name(f".{out_dir.name}.tmp-{token}")
    tmp.mkdir()
    try:
        shutil.copytree(ASSET_DIR, tmp / "assets", ignore=shutil.ignore_patterns("page.html", "__pycache__"))
        latest = _read_json(op / "latest.json")
        entries = _history_entries(op)

        # Which cycle directories exist (newest first), limited to keep the site small.
        cycles_dir = op / "cycles"
        existing = sorted((p.name for p in cycles_dir.iterdir() if p.is_dir()), reverse=True) if cycles_dir.is_dir() else []
        latest_rel = None
        if latest and isinstance(latest.get("path"), str):
            latest_rel = str(Path(latest["path"]).parent)
        elif existing:
            latest_rel = f"cycles/{existing[0]}"
        if latest_rel and not (op / latest_rel).is_dir():
            latest_rel = None
        latest_id = Path(latest_rel).name if latest_rel else None
        selected = list(existing[:MAX_CYCLE_PAGES])
        if latest_id and latest_id not in selected:
            selected.insert(0, latest_id)
        description_default = (
            "Experimental flood forecast prototype for the Sado river at Alcácer do Sal (BUR_PC project)."
            if "pt" not in i18n.langs
            else "Protótipo experimental de previsão de cheias do rio Sado em Alcácer do Sal (projeto BUR_PC)."
        )

        pages: dict[str, str] = {}
        if latest_rel:
            data = _load_cycle(op, latest_rel)
            content = _cycle_content(i18n, latest, data, "", latest_rel, is_latest=True, cycle_id=latest_id)
            updated = (latest or {}).get("updated_at") or data["status"].get("finished_at") or ""
            level = _level((latest or {}).get("level") or data["status"].get("level"))
            title = f"{site_title} – {i18n.plain('lvl_' + level)}"
            pages["index.html"] = _page(
                template, i18n, site_title=site_title, title=title, description=description_default,
                prefix="", current="index", content=content, updated=updated, base_url=base_url, rel_path="",
            )
            for cid in selected:
                if cid == latest_id:
                    continue
                rel = f"cycles/{cid}"
                cdata = _load_cycle(op, rel)
                if not cdata["status"]:
                    continue
                ccontent = _cycle_content(i18n, latest, cdata, "../../", rel, is_latest=False, cycle_id=cid)
                pages[f"cycles/{cid}/index.html"] = _page(
                    template, i18n, site_title=site_title, title=f"{site_title} – {cid}",
                    description=description_default, prefix="../../", current="index", content=ccontent,
                    updated="", base_url=base_url, rel_path=f"cycles/{cid}/",
                )
        else:
            pages["index.html"] = _page(
                template, i18n, site_title=site_title, title=f"{site_title} – {i18n.plain('no_data_t')}",
                description=description_default, prefix="", current="index", content=_starting_content(i18n),
                base_url=base_url, rel_path="",
            )

        page_ids = {Path(k).parent.name for k in pages if k.startswith("cycles/")} | ({latest_id} if latest_id else set())
        pages["history.html"] = _page(
            template, i18n, site_title=site_title, title=f"{site_title} – {i18n.plain('hist_h')}",
            description=description_default, prefix="", current="history",
            content=_history_content(i18n, entries, page_ids), base_url=base_url, rel_path="history.html",
        )
        pages["about.html"] = _page(
            template, i18n, site_title=site_title, title=f"{site_title} – {i18n.plain('about_h')}",
            description=description_default, prefix="", current="about", content=_about_content(i18n),
            base_url=base_url, rel_path="about.html",
        )
        if chat_endpoint:
            pages = {k: v.replace("<body ", f'<body data-chat="{_esc(chat_endpoint)}" ', 1) for k, v in pages.items()}
        for rel, text in pages.items():
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        # Cycle pages reference images through the cycle's data path relative to the page root.
        _copy_api(op, tmp / "api", selected)
        if latest_rel:
            (tmp / "api").mkdir(parents=True, exist_ok=True)
            (tmp / "api" / "chat_context.json").write_text(
                json.dumps(_chat_context(latest, _load_cycle(op, latest_rel), entries), ensure_ascii=False, indent=1),
                encoding="utf-8")
        if (op / "geometry").is_dir():  # light basin geometry for the animated maps
            shutil.copytree(op / "geometry", tmp / "geometry", dirs_exist_ok=True)
        # Image/overlay paths used by pages point to <prefix><cycles/<id>>/hydraulics/..., so expose
        # the copied cycle data at the same relative location.
        if (tmp / "api" / "cycles").is_dir():
            shutil.copytree(tmp / "api" / "cycles", tmp / "cycles", dirs_exist_ok=True)

        old = None
        if out_dir.exists():
            old = out_dir.with_name(f".{out_dir.name}.old-{token}")
            os.rename(out_dir, old)
        os.rename(tmp, out_dir)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return sorted(p for p in out_dir.rglob("*") if p.is_file())
