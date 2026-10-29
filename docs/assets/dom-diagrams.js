(function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  var renderQueued = false;

  function svgElement(name, attributes) {
    var node = document.createElementNS(SVG_NS, name);
    Object.keys(attributes || {}).forEach(function (key) {
      node.setAttribute(key, attributes[key]);
    });
    return node;
  }

  function pointFor(rect, stageRect, side) {
    var x = rect.left - stageRect.left;
    var y = rect.top - stageRect.top;
    if (side === "top") return { x: x + rect.width / 2, y: y };
    if (side === "bottom") return { x: x + rect.width / 2, y: y + rect.height };
    if (side === "left") return { x: x, y: y + rect.height / 2 };
    return { x: x + rect.width, y: y + rect.height / 2 };
  }

  function routeFor(fromRect, toRect, stageRect, requested, index) {
    function clamp(value, minimum, maximum) {
      return Math.min(maximum, Math.max(minimum, value));
    }

    var fromCenter = {
      x: fromRect.left - stageRect.left + fromRect.width / 2,
      y: fromRect.top - stageRect.top + fromRect.height / 2
    };
    var toCenter = {
      x: toRect.left - stageRect.left + toRect.width / 2,
      y: toRect.top - stageRect.top + toRect.height / 2
    };
    var dx = toCenter.x - fromCenter.x;
    var dy = toCenter.y - fromCenter.y;
    var route = requested || "auto";

    if (route === "right-loop" || route === "left-loop") {
      var loopRight = route === "right-loop";
      var startLoop = pointFor(fromRect, stageRect, loopRight ? "right" : "left");
      var endLoop = pointFor(toRect, stageRect, loopRight ? "right" : "left");
      var rawOutside = loopRight
        ? Math.max(startLoop.x, endLoop.x) + 48 + (index % 3) * 18
        : Math.min(startLoop.x, endLoop.x) - 48 - (index % 3) * 18;
      var outside = clamp(rawOutside, 18, stageRect.width - 18);
      var labelX = clamp(outside + (loopRight ? -104 : 104), 104, stageRect.width - 104);
      return {
        path: "M " + startLoop.x + " " + startLoop.y + " H " + outside + " V " + endLoop.y + " H " + endLoop.x,
        label: { x: labelX, y: (startLoop.y + endLoop.y) / 2 }
      };
    }

    if (route === "top-loop" || route === "bottom-loop") {
      var loopBottom = route === "bottom-loop";
      var startVertical = pointFor(fromRect, stageRect, loopBottom ? "bottom" : "top");
      var endVertical = pointFor(toRect, stageRect, loopBottom ? "bottom" : "top");
      var rawOutsideY = loopBottom
        ? Math.max(startVertical.y, endVertical.y) + 38 + (index % 3) * 16
        : Math.min(startVertical.y, endVertical.y) - 38 - (index % 3) * 16;
      var outsideY = clamp(rawOutsideY, 18, stageRect.height - 18);
      var labelY = clamp(outsideY + (loopBottom ? -24 : 24), 26, stageRect.height - 16);
      return {
        path: "M " + startVertical.x + " " + startVertical.y + " V " + outsideY + " H " + endVertical.x + " V " + endVertical.y,
        label: { x: (startVertical.x + endVertical.x) / 2, y: labelY }
      };
    }

    if (Math.abs(dy) >= Math.abs(dx) * 0.72) {
      var downward = dy >= 0;
      var start = pointFor(fromRect, stageRect, downward ? "bottom" : "top");
      var end = pointFor(toRect, stageRect, downward ? "top" : "bottom");
      var midY = (start.y + end.y) / 2;
      return {
        path: "M " + start.x + " " + start.y + " C " + start.x + " " + midY + ", " + end.x + " " + midY + ", " + end.x + " " + end.y,
        label: { x: (start.x + end.x) / 2, y: midY }
      };
    }

    var rightward = dx >= 0;
    var hStart = pointFor(fromRect, stageRect, rightward ? "right" : "left");
    var hEnd = pointFor(toRect, stageRect, rightward ? "left" : "right");
    var midX = (hStart.x + hEnd.x) / 2;
    return {
      path: "M " + hStart.x + " " + hStart.y + " C " + midX + " " + hStart.y + ", " + midX + " " + hEnd.y + ", " + hEnd.x + " " + hEnd.y,
      label: { x: midX, y: (hStart.y + hEnd.y) / 2 }
    };
  }

  function addDefinitions(svg, diagramId) {
    var defs = svgElement("defs");
    var colors = {
      "default": "#718078",
      success: "#165b47",
      agent: "#6c4aa1",
      warn: "#a66b16",
      loop: "#a66b16",
      error: "#ac4139",
      data: "#326594"
    };
    ["default", "success", "agent", "warn", "loop", "error", "data"].forEach(function (kind) {
      var marker = svgElement("marker", {
        id: diagramId + "-arrow-" + kind,
        markerWidth: "8",
        markerHeight: "8",
        refX: "7",
        refY: "4",
        orient: "auto",
        markerUnits: "strokeWidth"
      });
      marker.appendChild(svgElement("path", { d: "M 0 0 L 8 4 L 0 8 z", fill: colors[kind] }));
      defs.appendChild(marker);
    });
    svg.appendChild(defs);
  }

  function addLabel(svg, label, point, index) {
    if (!label) return;
    var width = Math.min(190, Math.max(42, label.length * 11 + 14));
    var x = point.x;
    var y = point.y - 7 - (index % 2) * 2;
    var background = svgElement("rect", {
      class: "edge-label-bg edge-item edge-index-" + index,
      x: String(x - width / 2),
      y: String(y - 12),
      width: String(width),
      height: "20"
    });
    var text = svgElement("text", {
      class: "edge-label edge-item edge-index-" + index,
      x: String(x),
      y: String(y + 2)
    });
    text.textContent = label;
    svg.appendChild(background);
    svg.appendChild(text);
  }

  function renderDiagram(diagram) {
    var stage = diagram.querySelector(".diagram-stage");
    var svg = diagram.querySelector(".connector-layer");
    var edgeItems = Array.from(diagram.querySelectorAll(".edge-model > li[data-from][data-to]"));
    if (!stage || !svg) return;

    var stageRect = stage.getBoundingClientRect();
    var width = stage.scrollWidth;
    var height = stage.scrollHeight;
    svg.replaceChildren();
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    addDefinitions(svg, diagram.id || "diagram");

    edgeItems.forEach(function (edge, index) {
      var from = document.getElementById(edge.dataset.from);
      var to = document.getElementById(edge.dataset.to);
      if (!from || !to || !stage.contains(from) || !stage.contains(to)) return;
      var kind = edge.dataset.kind || "default";
      var routed = routeFor(from.getBoundingClientRect(), to.getBoundingClientRect(), stageRect, edge.dataset.route, index);
      var path = svgElement("path", {
        class: "edge-path edge-" + kind + " edge-item edge-index-" + index,
        d: routed.path,
        "data-from": edge.dataset.from,
        "data-to": edge.dataset.to,
        "marker-end": "url(#" + (diagram.id || "diagram") + "-arrow-" + kind + ")"
      });
      svg.appendChild(path);
      addLabel(svg, edge.dataset.label || "", routed.label, index);
    });
  }

  function clearFocus(diagram) {
    delete diagram.dataset.focusedNode;
    diagram.querySelectorAll(".flow-node, .edge-item").forEach(function (node) {
      node.classList.remove("is-related", "is-dimmed");
    });
  }

  function focusNode(diagram, node) {
    var id = node.id;
    if (diagram.dataset.focusedNode === id) {
      clearFocus(diagram);
      return;
    }
    diagram.dataset.focusedNode = id;
    var relatedIds = new Set([id]);
    var edgeIndexes = new Set();
    Array.from(diagram.querySelectorAll(".edge-model > li[data-from][data-to]")).forEach(function (edge, index) {
      if (edge.dataset.from === id || edge.dataset.to === id) {
        relatedIds.add(edge.dataset.from);
        relatedIds.add(edge.dataset.to);
        edgeIndexes.add(index);
      }
    });
    diagram.querySelectorAll(".flow-node").forEach(function (candidate) {
      candidate.classList.toggle("is-related", relatedIds.has(candidate.id));
      candidate.classList.toggle("is-dimmed", !relatedIds.has(candidate.id));
    });
    diagram.querySelectorAll(".edge-item").forEach(function (edgeGraphic) {
      var match = Array.from(edgeIndexes).some(function (edgeIndex) {
        return edgeGraphic.classList.contains("edge-index-" + edgeIndex);
      });
      edgeGraphic.classList.toggle("is-related", match);
      edgeGraphic.classList.toggle("is-dimmed", !match);
    });
  }

  function enhanceDiagram(diagram, index) {
    if (!diagram.id) diagram.id = "dom-diagram-" + index;
    var stage = diagram.querySelector(".diagram-stage");
    if (stage) {
      stage.addEventListener("click", function (event) {
        if (!event.target.closest(".flow-node")) clearFocus(diagram);
      });
    }

    diagram.querySelectorAll(".flow-node[id]").forEach(function (node) {
      node.tabIndex = 0;
      node.setAttribute("title", "点击或按 Enter，高亮直接关系");
      node.addEventListener("click", function () { focusNode(diagram, node); });
      node.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          focusNode(diagram, node);
        }
      });
    });
  }

  function queueRender() {
    if (renderQueued) return;
    renderQueued = true;
    window.requestAnimationFrame(function () {
      document.querySelectorAll(".diagram").forEach(renderDiagram);
      renderQueued = false;
    });
  }

  function initializePageChrome() {
    var progress = document.createElement("div");
    progress.className = "reading-progress";
    progress.setAttribute("aria-hidden", "true");
    document.body.appendChild(progress);

    var topButton = document.createElement("button");
    topButton.type = "button";
    topButton.className = "back-to-top";
    topButton.textContent = "↑";
    topButton.title = "返回顶部";
    topButton.setAttribute("aria-label", "返回顶部");
    topButton.addEventListener("click", function () { window.scrollTo({ top: 0, behavior: "smooth" }); });
    document.body.appendChild(topButton);

    function updateScrollUi() {
      var root = document.documentElement;
      var maximum = Math.max(1, root.scrollHeight - root.clientHeight);
      progress.style.width = Math.min(100, window.scrollY / maximum * 100) + "%";
      topButton.classList.toggle("visible", window.scrollY > 520);
    }
    updateScrollUi();
    window.addEventListener("scroll", updateScrollUi, { passive: true });

    if ("IntersectionObserver" in window) {
      var links = new Map();
      document.querySelectorAll('.page-nav a[href^="#"]').forEach(function (link) {
        links.set(link.getAttribute("href").slice(1), link);
      });
      var observer = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          links.forEach(function (link) { link.classList.remove("active"); });
          var active = links.get(entry.target.id);
          if (active) active.classList.add("active");
        });
      }, { rootMargin: "-18% 0px -74% 0px", threshold: 0 });
      document.querySelectorAll("main > section[id]").forEach(function (section) { observer.observe(section); });
    }
  }

  function initialize() {
    initializePageChrome();
    document.querySelectorAll(".diagram").forEach(enhanceDiagram);
    queueRender();
    window.addEventListener("resize", queueRender, { passive: true });
    window.addEventListener("load", queueRender, { once: true });
    if ("ResizeObserver" in window) {
      var resizeObserver = new ResizeObserver(queueRender);
      document.querySelectorAll(".diagram-stage").forEach(function (stage) { resizeObserver.observe(stage); });
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize);
  else initialize();
})();
