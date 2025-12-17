from PIL import Image
import os

def make_transparent(img_path, output_path):
    img = Image.open(img_path).convert("RGBA")
    datas = img.getdata()

    newData = []
    for item in datas:
        # If the pixel is very dark (close to black), make it transparent
        # Threshold of 20 to handle slight artifacts
        if item[0] < 20 and item[1] < 20 and item[2] < 20:
            newData.append((255, 255, 255, 0))
        else:
            newData.append(item)

    img.putdata(newData)
    img.save(output_path, "PNG")

if __name__ == "__main__":
    input_file = r"C:\Users\schoo\.gemini\antigravity\brain\ac8df07f-5da4-45af-a162-9c56eb1bcf88\uploaded_image_1766009014266.png"
    output_file = r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\esign\logo.png"
    make_transparent(input_file, output_file)
    print(f"Successfully created transparent logo at {output_file}")
