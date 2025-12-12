import re
import os


def extract_d_codes(input_file, output_file):
    # 检查输入文件是否存在
    if not os.path.exists(input_file):
        print(f"错误：找不到文件 '{input_file}'，请确保该文件在当前目录下。")
        return

    try:
        # 读取文件内容
        with open(input_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # 使用正则表达式提取代码
        # (?i) 表示忽略大小写 (匹配 d_ 和 D_)
        # d_ 匹配字面量
        # \d+ 匹配一个或多个数字
        matches = re.findall(r'(?i)d_\d+', content)

        # 数据清洗：
        # 1. m.lower() 将所有代码统一为小写 (d_xxxx)，防止 D_123 和 d_123 被视为两个不同的码
        # 2. set() 去除重复项
        # 3. sorted() 对结果进行排序
        unique_codes = sorted(list(set(m.lower() for m in matches)))

        # 将结果写入输出文件
        with open(output_file, 'w', encoding='utf-8') as f:
            for code in unique_codes:
                f.write(code + '\n')

        print(f"处理完成！")
        print(f"原始匹配数量: {len(matches)}")
        print(f"去重后数量: {len(unique_codes)}")
        print(f"结果已保存至: {output_file}")

    except Exception as e:
        print(f"发生未知错误: {e}")


if __name__ == "__main__":
    # 定义输入和输出文件名
    input_filename = 'd_org_code.txt'
    output_filename = 'd_code.txt'

    extract_d_codes(input_filename, output_filename)
