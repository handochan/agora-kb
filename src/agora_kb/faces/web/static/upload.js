// Drag-and-drop multi-upload wiring for the capture form (ADR-0025). Vanilla JS, no Node/CDN
// (ADR-0019 §7 vendoring posture). Progressive enhancement: the <input type="file" multiple> and
// the HTMX multipart POST work without JS; this only adds the drop-zone UX (drop → fill the input
// + render the chosen-file list). All DOM text is set via textContent (never innerHTML) so a file
// name can never inject markup.
(function () {
  "use strict";

  function init() {
    var dropzone = document.getElementById("dropzone");
    var input = document.getElementById("file");
    var list = document.getElementById("file-list");
    if (!dropzone || !input || !list) {
      return;
    }

    function renderList() {
      list.replaceChildren();
      var files = input.files;
      if (!files || files.length === 0) {
        return;
      }
      for (var i = 0; i < files.length; i++) {
        var li = document.createElement("li");
        li.textContent = files[i].name + " (" + files[i].size + " bytes)";
        list.appendChild(li);
      }
    }

    // Click anywhere in the zone opens the native picker (but not when the input itself is clicked,
    // which would double-trigger).
    dropzone.addEventListener("click", function (ev) {
      if (ev.target !== input) {
        input.click();
      }
    });
    dropzone.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        input.click();
      }
    });

    ["dragenter", "dragover"].forEach(function (name) {
      dropzone.addEventListener(name, function (ev) {
        ev.preventDefault();
        dropzone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      dropzone.addEventListener(name, function (ev) {
        ev.preventDefault();
        dropzone.classList.remove("dragover");
      });
    });

    dropzone.addEventListener("drop", function (ev) {
      if (ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files.length) {
        input.files = ev.dataTransfer.files; // fill the real input so HTMX/multipart posts them
        renderList();
      }
    });

    input.addEventListener("change", renderList);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
