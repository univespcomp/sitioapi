// Configuração do Chart.js para Dark Theme
Chart.defaults.color = "#94a3b8";
Chart.defaults.font.family = "'Inter', sans-serif";
Chart.defaults.scale.grid.color = "rgba(255, 255, 255, 0.05)";

// Elementos da DOM
const elTempAmanha = document.getElementById("kpi-temp-amanha");
const elDataAmanha = document.getElementById("kpi-data-amanha");
const elTempHoje = document.getElementById("kpi-temp-hoje");
const elDataHoje = document.getElementById("kpi-data-hoje");
const elUmidHoje = document.getElementById("kpi-umid-hoje");
const elUltimaAtt = document.getElementById("kpi-ultima-att");
const elMetodo = document.getElementById("kpi-metodo");

async function loadData() {
  try {
    // Tentamos carregar os arquivos CSV da pasta mãe
    const previsoesResp = await fetch("/historico_previsoes_operacionais.csv");
    const atualizadoResp = await fetch(
      "/presidente_prudente_A707_dados_atualizados.csv",
    );

    let temPrevisoes = previsoesResp.ok;
    let temAtualizado = atualizadoResp.ok;

    if (temPrevisoes) {
      const text = await previsoesResp.text();
      Papa.parse(text, {
        header: true,
        delimiter: ";",
        skipEmptyLines: true,
        complete: function (results) {
          processarPrevisoes(results.data);
        },
      });
    }

    if (temAtualizado) {
      const text = await atualizadoResp.text();
      Papa.parse(text, {
        header: true,
        delimiter: ";",
        skipEmptyLines: true,
        complete: function (results) {
          processarHistorico(results.data);
        },
      });
    }

    if (!temPrevisoes && !temAtualizado) {
      console.warn(
        "Arquivos CSV não encontrados. O script PI IV.py precisa ser executado para gerar os dados.",
      );
      elTempAmanha.textContent = "Sem dados";
      elDataAmanha.textContent = "Execute o Python";
    }
  } catch (e) {
    console.error("Erro ao carregar dados:", e);
  }
}

function formatarDataBR(dataStr) {
  if (!dataStr) return "--/--/----";
  const partes = dataStr.split("-");
  if (partes.length === 3) {
    return `${partes[2]}/${partes[1]}/${partes[0]}`;
  }
  return dataStr;
}

function processarPrevisoes(dados) {
  if (!dados || dados.length === 0) return;

  // A última previsão
  const ultima = dados[dados.length - 1];

  elTempAmanha.textContent = `${parseFloat(ultima.temperatura_prevista_c).toFixed(1)} °C`;
  elDataAmanha.textContent = `Data Alvo: ${formatarDataBR(ultima.data_prevista)}`;

  elTempHoje.textContent = `${parseFloat(ultima.temp_media_c).toFixed(1)} °C`;
  elDataHoje.textContent = `Data Base: ${formatarDataBR(ultima.data_base)}`;

  elUmidHoje.textContent = `${parseFloat(ultima.umidade_media_pct).toFixed(1)} %`;

  const emitida = ultima.emitida_em ? ultima.emitida_em.split(" ")[0] : "--";
  elUltimaAtt.textContent = formatarDataBR(emitida);

  // Simplificar o texto do método
  let metodo = ultima.metodo_entrada || "--";
  if (metodo === "observacao_real") metodo = "Observação Real";
  else if (metodo === "media_12_meses") metodo = "Média 12 Meses";
  elMetodo.textContent = metodo;

  // Pegar os últimos 30 dias para o gráfico
  const ultimos30 = dados.slice(-30);
  const labels = ultimos30.map((d) =>
    formatarDataBR(d.data_prevista).substring(0, 5),
  );
  const tempsBase = ultimos30.map((d) => parseFloat(d.temp_media_c));
  const tempsPrevistas = ultimos30.map((d) =>
    parseFloat(d.temperatura_prevista_c),
  );

  renderPrevisaoChart(labels, tempsBase, tempsPrevistas);
}

function processarHistorico(dados) {
  if (!dados || dados.length === 0) return;

  // Pega as últimas observações. Filtrar onde tem dados de fato.
  const validos = dados.filter(
    (d) =>
      !isNaN(parseFloat(d.umidade_media_pct)) &&
      !isNaN(parseFloat(d.precipitacao_mm)),
  );
  const ultimos30 = validos.slice(-30);

  const labels = ultimos30.map((d) => formatarDataBR(d.data).substring(0, 5));
  const umidade = ultimos30.map((d) => parseFloat(d.umidade_media_pct));
  const precipitacao = ultimos30.map((d) => parseFloat(d.precipitacao_mm));

  renderCondicoesChart(labels, umidade, precipitacao);
}

let chartPrev = null;
function renderPrevisaoChart(labels, base, prevista) {
  const ctx = document.getElementById("previsaoChart").getContext("2d");

  if (chartPrev) {
    chartPrev.destroy();
  }

  const gradientPrevista = ctx.createLinearGradient(0, 0, 0, 400);
  gradientPrevista.addColorStop(0, "rgba(139, 92, 246, 0.5)");
  gradientPrevista.addColorStop(1, "rgba(139, 92, 246, 0.0)");

  chartPrev = new Chart(ctx, {
    type: "line",
    data: {
      labels: labels,
      datasets: [
        {
          label: "Temp. Prevista (°C)",
          data: prevista,
          borderColor: "#8b5cf6",
          backgroundColor: gradientPrevista,
          borderWidth: 2,
          pointBackgroundColor: "#8b5cf6",
          pointBorderColor: "#fff",
          pointRadius: 4,
          pointHoverRadius: 6,
          fill: true,
          tension: 0.4,
        },
        {
          label: "Temp. Observada (D-1)",
          data: base,
          borderColor: "#3b82f6",
          borderWidth: 2,
          borderDash: [5, 5],
          pointRadius: 0,
          pointHoverRadius: 4,
          fill: false,
          tension: 0.4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: "top",
          labels: { boxWidth: 12, usePointStyle: true },
        },
        tooltip: {
          backgroundColor: "rgba(15, 23, 42, 0.9)",
          titleColor: "#fff",
          bodyColor: "#cbd5e1",
          borderColor: "rgba(255,255,255,0.1)",
          borderWidth: 1,
          padding: 10,
        },
      },
      scales: {
        y: {
          beginAtZero: false,
          suggestedMin: 15,
          suggestedMax: 35,
        },
      },
    },
  });
}

let chartCond = null;
function renderCondicoesChart(labels, umidade, precipitacao) {
  const ctx = document.getElementById("condicoesChart").getContext("2d");

  if (chartCond) {
    chartCond.destroy();
  }

  chartCond = new Chart(ctx, {
    type: "bar",
    data: {
      labels: labels,
      datasets: [
        {
          label: "Precipitação (mm)",
          data: precipitacao,
          backgroundColor: "rgba(59, 130, 246, 0.7)",
          borderColor: "#3b82f6",
          borderWidth: 1,
          yAxisID: "y1",
        },
        {
          type: "line",
          label: "Umidade Média (%)",
          data: umidade,
          borderColor: "#10b981",
          backgroundColor: "rgba(16, 185, 129, 0.1)",
          borderWidth: 2,
          pointRadius: 2,
          fill: true,
          tension: 0.4,
          yAxisID: "y",
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: "top",
          labels: { boxWidth: 12, usePointStyle: true },
        },
      },
      scales: {
        y: {
          type: "linear",
          display: true,
          position: "left",
          beginAtZero: true,
          max: 100,
          title: { display: true, text: "Umidade (%)", color: "#94a3b8" },
        },
        y1: {
          type: "linear",
          display: true,
          position: "right",
          beginAtZero: true,
          suggestedMax: 50,
          grid: { drawOnChartArea: false },
          title: { display: true, text: "Chuva (mm)", color: "#94a3b8" },
        },
      },
    },
  });
}

// Iniciar carregamento
document.addEventListener("DOMContentLoaded", loadData);
