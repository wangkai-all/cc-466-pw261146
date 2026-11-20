/* 依赖图渲染：分层布局 + SVG。颜色按角色分组（契约/外部表/应用下游），
   细分类靠形状与文字标签区分；边样式表示关键级别。 */
(function () {
  "use strict";

  var COLOR_BY_GROUP = {
    contract: "var(--series-1)",
    table: "var(--series-3)",
    app: "var(--series-2)"
  };
  var KIND_GROUP = { contract: "contract", table: "table", dashboard: "app", job: "app", api: "app" };
  var KIND_LABEL = { contract: "契约", table: "表", dashboard: "看板", job: "任务", api: "API" };
  var KIND_SHAPE = { contract: "rrect", table: "rect", dashboard: "diamond", job: "triangle", api: "hex" };

  function svgEl(tag, attrs) {
    var el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }

  function computeLayers(nodes, edges, focusSource) {
    var indeg = {}, adj = {}, names = nodes.map(function (n) { return n.name; });
    names.forEach(function (n) { indeg[n] = 0; adj[n] = []; });
    edges.forEach(function (e) {
      if (indeg[e.target] !== undefined && adj[e.source] !== undefined) {
        indeg[e.target]++; adj[e.source].push(e.target);
      }
    });
    var layer = {};
    if (focusSource) {
      // 影响视图：以变更契约为第 0 层，按 BFS 深度分层
      layer[focusSource] = 0;
      var q = [focusSource];
      while (q.length) {
        var cur = q.shift();
        (adj[cur] || []).forEach(function (t) {
          if (layer[t] === undefined || layer[t] > layer[cur] + 1) {
            layer[t] = layer[cur] + 1; q.push(t);
          }
        });
      }
      names.forEach(function (n) { if (layer[n] === undefined) layer[n] = 0; });
    } else {
      // 全图：最长路径分层，源节点为第 0 层
      var roots = names.filter(function (n) { return indeg[n] === 0; });
      if (!roots.length) roots = names.slice(0, 1);
      var order = [], indeg2 = Object.assign({}, indeg), qq = roots.slice();
      roots.forEach(function (r) { layer[r] = 0; });
      while (qq.length) {
        var c = qq.shift(); order.push(c);
        (adj[c] || []).forEach(function (t) {
          layer[t] = Math.max(layer[t] || 0, (layer[c] || 0) + 1);
          if (--indeg2[t] === 0) qq.push(t);
        });
      }
      names.forEach(function (n) { if (layer[n] === undefined) layer[n] = 0; });
    }
    return layer;
  }

  function drawShape(svg, shape, x, y, fill, stroke, strokeW) {
    var el;
    if (shape === "rrect") {
      el = svgEl("rect", { x: x - 16, y: y - 16, width: 32, height: 32, rx: 8 });
    } else if (shape === "rect") {
      el = svgEl("rect", { x: x - 15, y: y - 15, width: 30, height: 30, rx: 2 });
    } else if (shape === "diamond") {
      el = svgEl("polygon", { points: [x, y - 18, x + 18, y, x, y + 18, x - 18, y].join(",") });
    } else if (shape === "triangle") {
      el = svgEl("polygon", { points: [x, y - 17, x + 17, y + 14, x - 17, y + 14].join(",") });
    } else { // hex
      el = svgEl("polygon", {
        points: [x - 9, y - 16, x + 9, y - 16, x + 17, y, x + 9, y + 16, x - 9, y + 16, x - 17, y].join(",")
      });
    }
    el.setAttribute("fill", fill);
    el.setAttribute("stroke", stroke || "none");
    el.setAttribute("stroke-width", strokeW || 0);
    svg.appendChild(el);
    return el;
  }

  window.renderDepGraph = function (container, data, opts) {
    opts = opts || {};
    var highlight = opts.highlight || null; // {source, affected: {name: depth}}
    var nodes = data.nodes, edges = data.edges;
    if (!nodes.length) { container.textContent = "暂无依赖数据"; return; }

    var layer = computeLayers(nodes, edges, highlight && highlight.source);
    var byLayer = {};
    nodes.forEach(function (n) {
      var l = layer[n.name] || 0;
      (byLayer[l] = byLayer[l] || []).push(n);
    });
    var layers = Object.keys(byLayer).map(Number).sort(function (a, b) { return a - b; });
    var maxRows = Math.max.apply(null, layers.map(function (l) { return byLayer[l].length; }));

    var colW = 230, rowH = 96, padX = 90, padY = 56;
    var W = padX * 2 + (layers.length - 1) * colW;
    var H = padY * 2 + (maxRows - 1) * rowH;
    var pos = {};
    layers.forEach(function (l) {
      var arr = byLayer[l];
      var totalH = (arr.length - 1) * rowH;
      arr.forEach(function (n, i) {
        pos[n.name] = { x: padX + layers.indexOf(l) * colW, y: padY + (H - 2 * padY) / 2 - totalH / 2 + i * rowH };
      });
    });

    var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, role: "img",
                             "aria-label": opts.ariaLabel || "依赖关系图" });
    var defs = svgEl("defs", {});
    var marker = svgEl("marker", { id: container.id + "-arrow", viewBox: "0 0 10 10",
      refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
    marker.appendChild(svgEl("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "var(--text-muted)" }));
    defs.appendChild(marker);
    svg.appendChild(defs);

    // 边
    edges.forEach(function (e) {
      var s = pos[e.source], t = pos[e.target];
      if (!s || !t) return;
      var mx = (s.x + t.x) / 2;
      var path = svgEl("path", {
        d: "M " + (s.x + 20) + " " + s.y + " C " + mx + " " + s.y + ", " + mx + " " + t.y +
           ", " + (t.x - 22) + " " + t.y,
        fill: "none",
        stroke: "var(--text-muted)",
        "stroke-width": e.criticality === "high" ? 2.2 : 1.4,
        "stroke-dasharray": e.criticality === "low" ? "5 4" : "none",
        opacity: highlight && !(highlight.affected[e.target] !== undefined && e.source === highlight.source
          || highlight.affected[e.source] !== undefined && highlight.affected[e.target] !== undefined) ? 0.25 : 0.85,
        "marker-end": "url(#" + container.id + "-arrow)"
      });
      svg.appendChild(path);
    });

    // 节点
    var tooltip = container.parentElement.querySelector(".graph-tooltip");
    nodes.forEach(function (n) {
      var p = pos[n.name];
      var group = KIND_GROUP[n.kind] || "app";
      var affected = !highlight || n.name === highlight.source || highlight.affected[n.name] !== undefined;
      var g = svgEl("g", { opacity: affected ? 1 : 0.3, cursor: n.kind === "contract" ? "pointer" : "default" });

      var isSource = highlight && n.name === highlight.source;
      var ringColor = isSource ? "var(--status-critical)"
        : (highlight && highlight.affected[n.name] !== undefined ? "var(--status-warning)" : "none");
      if (ringColor !== "none") {
        var ring = svgEl("circle", { cx: p.x, cy: p.y, r: 24, fill: "none",
                                     stroke: ringColor, "stroke-width": 2.5 });
        g.appendChild(ring);
      }
      drawShape(g, KIND_SHAPE[n.kind] || "rect", p.x, p.y, COLOR_BY_GROUP[group],
                "var(--surface-1)", 2);

      var label = svgEl("text", { x: p.x, y: p.y + 34, "text-anchor": "middle",
                                  "font-size": 12, fill: "var(--text-primary)", "font-weight": 600 });
      label.textContent = n.name.length > 24 ? n.name.slice(0, 23) + "…" : n.name;
      g.appendChild(label);
      var cap = svgEl("text", { x: p.x, y: p.y + 48, "text-anchor": "middle",
                                "font-size": 10.5, fill: "var(--text-muted)" });
      cap.textContent = KIND_LABEL[n.kind] || n.kind;
      g.appendChild(cap);

      g.addEventListener("mouseenter", function (ev) {
        if (!tooltip) return;
        tooltip.innerHTML = "";
        var t = document.createElement("div"); t.className = "tt-title"; t.textContent = n.name;
        var r1 = document.createElement("div"); r1.className = "tt-row";
        r1.textContent = "类型：" + (KIND_LABEL[n.kind] || n.kind);
        var r2 = document.createElement("div"); r2.className = "tt-row";
        r2.textContent = (n.kind === "contract" ? "负责人：" : "团队：") + (n.owner || "—");
        tooltip.appendChild(t); tooltip.appendChild(r1); tooltip.appendChild(r2);
        if (highlight && highlight.affected[n.name] !== undefined) {
          var r3 = document.createElement("div"); r3.className = "tt-row";
          r3.textContent = "影响深度：第 " + highlight.affected[n.name] + " 层下游";
          tooltip.appendChild(r3);
        }
        var rect = container.getBoundingClientRect();
        tooltip.style.display = "block";
        tooltip.style.left = (ev.clientX - rect.left + 14) + "px";
        tooltip.style.top = (ev.clientY - rect.top + 10) + "px";
      });
      g.addEventListener("mouseleave", function () { if (tooltip) tooltip.style.display = "none"; });
      if (n.kind === "contract") {
        g.addEventListener("click", function () { location.href = "/contracts/" + n.name; });
      }
      svg.appendChild(g);
    });

    container.appendChild(svg);
  };
})();
