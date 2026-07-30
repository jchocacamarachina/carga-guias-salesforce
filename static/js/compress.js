// Comprime una foto de cámara antes de guardarla/subirla: reduce dimensiones y
// re-codifica a JPEG con calidad media. Corre en la página (no en el Service Worker).
async function compressImage(file, { maxDim = 1600, quality = 0.75 } = {}) {
  // imageOrientation explícito: sin esto, algunos navegadores descartan la rotación EXIF
  // al decodificar y la foto queda "acostada" una vez recomprimida.
  const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });

  let { width, height } = bitmap;
  if (width > maxDim || height > maxDim) {
    if (width >= height) {
      height = Math.round(height * (maxDim / width));
      width = maxDim;
    } else {
      width = Math.round(width * (maxDim / height));
      height = maxDim;
    }
  }

  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(bitmap, 0, 0, width, height);
  bitmap.close();

  const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality));

  // Si por algún motivo la compresión no ayudó (imagen ya pequeña/simple), usar el original.
  if (!blob || blob.size >= file.size) {
    return file;
  }
  return blob;
}
