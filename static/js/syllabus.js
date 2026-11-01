(() => {
  "use strict";

  async function renderSyllabus(viewer) {
    const status = viewer.querySelector('[role="status"]');
    const pages = viewer.querySelector(".syllabus-pages");
    try {
      if (!window.pdfjsLib) {
        throw new Error("The PDF page renderer did not load.");
      }
      window.pdfjsLib.GlobalWorkerOptions.workerSrc =
        "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
      const pdf = await window.pdfjsLib.getDocument(viewer.dataset.pdfUrl).promise;
      let renderedPages = 0;
      for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber += 1) {
        status.textContent = `Loading page ${pageNumber} of ${pdf.numPages}…`;
        const page = await pdf.getPage(pageNumber);
        const initialViewport = page.getViewport({ scale: 1 });
        const scale = Math.min(1.4, 1000 / initialViewport.width);
        const viewport = page.getViewport({ scale });
        const canvas = document.createElement("canvas");
        canvas.setAttribute(
          "aria-label",
          `${viewer.dataset.pdfTitle}, page ${pageNumber} of ${pdf.numPages}`
        );
        canvas.style.display = "block";
        canvas.style.width = "100%";
        canvas.style.height = "auto";
        canvas.style.margin = "0 auto 18px";
        canvas.width = Math.ceil(viewport.width);
        canvas.height = Math.ceil(viewport.height);
        const context = canvas.getContext("2d", { alpha: false });
        if (!context) {
          throw new Error("Could not prepare the syllabus page preview.");
        }
        pages.appendChild(canvas);
        page.render({ canvasContext: context, viewport }).promise.then(() => {
          renderedPages += 1;
          status.textContent = `Rendered ${renderedPages} of ${pdf.numPages} syllabus pages.`;
        }).catch((error) => {
          console.error(`Could not render syllabus page ${pageNumber}.`, error);
          status.textContent = `Page ${pageNumber} could not be displayed. Open the PDF instead.`;
        });
      }
      status.textContent = `Preparing ${pdf.numPages} syllabus pages…`;
    } catch (error) {
      console.error("Could not render syllabus PDF pages.", error);
      status.textContent = "The page preview could not be loaded. Open the PDF to view it.";
    }
  }

  document.querySelectorAll(".syllabus-viewer").forEach(renderSyllabus);
})();
