import argparse

def parse_time(time_str):
    time_parts = time_str.replace(',', '.').split(':')
    h = int(time_parts[0])
    m = int(time_parts[1])
    s = float(time_parts[2])
    return h * 3600 + m * 60 + s

def format_time(total_seconds):
    total_seconds = max(total_seconds, 0.0)
    hours = int(total_seconds // 3600)
    remainder = total_seconds % 3600
    minutes = int(remainder // 60)
    seconds = remainder % 60
    milliseconds = int(round((seconds - int(seconds)) * 1000))
    seconds = int(seconds)
    milliseconds = max(0, min(milliseconds, 999))
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

def detect_encoding(file_path):
    encodings = ['utf-8-sig', 'iso-8859-1', 'windows-1252', 'cp1252']
    for encoding in encodings:
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                f.read(1024)
            return encoding
        except UnicodeDecodeError:
            continue
    return 'utf-8'  # Fallback padrão

def adjust_srt(input_path, output_path, offset):
    encoding = detect_encoding(input_path)
    
    with open(input_path, 'r', encoding=encoding) as infile:
        lines = infile.readlines()
    
    with open(output_path, 'w', encoding='utf-8') as outfile:
        for line in lines:
            if '-->' in line:
                parts = line.strip().split(' --> ')
                if len(parts) != 2:
                    outfile.write(line)
                    continue
                
                start_str, end_str = parts
                start = parse_time(start_str)
                end = parse_time(end_str)
                
                adjusted_start = max(start + offset, 0.0)
                adjusted_end = max(end + offset, 0.0)
                
                if adjusted_end < adjusted_start:
                    adjusted_end = adjusted_start
                
                new_start = format_time(adjusted_start)
                new_end = format_time(adjusted_end)
                
                new_line = f"{new_start} --> {new_end}\n"
                outfile.write(new_line)
            else:
                outfile.write(line)

if __name__ == "__main__":
    
    parser = argparse.ArgumentParser(description='Ler um arquivo de parâmetros JSON.')
    parser.add_argument('-I','--input', type=str, help='Caminho do arquivo SRT de entrada')
    parser.add_argument('-O','--output', type=str, help='Caminho do arquivo SRT de saída')
    parser.add_argument('-T','--time', type=str, help='Tempo de atraso (negativo) ou adiantamento (positivo) em segundos. ') 
    args = parser.parse_args()
    print(args)

    if not (args.input and args.output and args.time):
        exit(1)

    input_path = args.input
    output_path = args.output
    offset_input = (args.time).strip().lower()
    
    offset_str = offset_input.replace('s', '').strip()
    try:
        offset = float(offset_str)
        print("Parâmetros lidos com sucesso!")
    except ValueError:
        print("Erro: Deslocamento inválido. Deve ser um número como 0.5 ou -1.3")
        exit(1)
    
    adjust_srt(input_path, output_path, offset)
    print(f"Arquivo ajustado salvo em {output_path}")