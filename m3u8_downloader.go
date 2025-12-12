package main

import (
	"bufio"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

// 全局配置结构体
type Config struct {
	YtDlpThreads  string // yt-dlp -N 参数
	TargetDir     string // 最终目标文件夹 (rclone)
	TempDir       string // 临时下载文件夹
	MaxConcurrent int    // 同时执行的任务数
}

var (
	fileMutex sync.Mutex    // 用于文件写入的互斥锁
	linkFile  = "aria2_links.txt"
	allLinks  []string      // 内存中保存的所有链接
)

func main() {
	printHeader()

	// 1. 获取配置
	config := getUserConfig()

	// 2. 确保文件夹存在
	ensureDir(config.TempDir)
	ensureDir(config.TargetDir)

	// 3. 获取链接
	links := getLinks()
	if len(links) == 0 {
		fmt.Println("没有检测到有效的下载链接，程序退出。")
		return
	}
	allLinks = links // 初始化内存中的列表

	fmt.Printf("\n开始处理 %d 个任务...\n", len(links))
	fmt.Println("------------------------------------------------")

	// 4. 初始化工作池
	jobs := make(chan string, len(links))
	var wg sync.WaitGroup

	// 启动 Worker
	for i := 0; i < config.MaxConcurrent; i++ {
		wg.Add(1)
		go worker(i+1, jobs, &wg, config)
	}

	// 发送任务
	for _, link := range links {
		jobs <- link
	}
	close(jobs)

	// 等待所有任务完成
	wg.Wait()

	fmt.Println("\n------------------------------------------------")
	fmt.Println("所有任务处理完毕！")
}

// worker 处理具体的下载逻辑 (修复了编码问题)
func worker(id int, jobs <-chan string, wg *sync.WaitGroup, config Config) {
	defer wg.Done()

	for link := range jobs {
		link = strings.TrimSpace(link)
		if link == "" {
			continue
		}

		// 1. 解析目标文件名 (从 URL 中获取，避免控制台乱码)
		finalFileName := getFileNameFromURL(link)
		fmt.Printf("[Worker %d] 识别任务: %s\n", id, finalFileName)

		// 2. 设置临时的安全文件名 (避免下载过程中出现特殊字符错误)
		// 格式: temp_<workerID>_<timestamp>.mp4
		tempSafeName := fmt.Sprintf("temp_%d_%d.mp4", id, time.Now().UnixNano())
		tempSafePath := filepath.Join(config.TempDir, tempSafeName)

		// 3. 下载视频
		// 使用 -o 指定绝对的临时路径
		downloadArgs := []string{
			"-N", config.YtDlpThreads,
			"-o", tempSafePath,
			link,
		}
		
		cmdDownload := exec.Command("yt-dlp", downloadArgs...)
		// 如果想看详细日志可以解开下面这行
		// cmdDownload.Stdout = os.Stdout
		
		err := cmdDownload.Run()
		if err != nil {
			fmt.Printf("[Worker %d] 下载失败: %s (错误: %v)\n", id, finalFileName, err)
			continue
		}

		fmt.Printf("[Worker %d] 下载完成，正在归档 -> %s\n", id, finalFileName)

		// 4. 移动并重命名 (Temp -> Target)
		finalPath := filepath.Join(config.TargetDir, finalFileName)
		
		err = moveFile(tempSafePath, finalPath)
		if err != nil {
			fmt.Printf("[Worker %d] 移动文件失败: %v\n", id, err)
			// 尝试清理临时文件（如果存在）
			os.Remove(tempSafePath) 
			continue
		}

		fmt.Printf("[Worker %d] 成功处理: %s\n", id, finalFileName)

		// 5. 从文件中删除该链接
		removeLinkFromFile(link)
	}
}

// getFileNameFromURL 从 URL 中解析并解码文件名
func getFileNameFromURL(link string) string {
	u, err := url.Parse(link)
	if err != nil {
		// 如果解析失败，返回一个随机名
		return fmt.Sprintf("unknown_%d.mp4", time.Now().Unix())
	}

	// 获取路径的最后一部分 (例如 18.视觉触发.m3u8)
	baseName := filepath.Base(u.Path)
	
	// URL 解码 (将 %E8%A7... 转换为 中文)
	decodedName, err := url.QueryUnescape(baseName)
	if err != nil {
		decodedName = baseName
	}

	// 移除 .m3u8 后缀，添加 .mp4
	// 注意：这里假设下载的是视频，如果 yt-dlp 自动合并，通常是 mp4 或 mkv
	nameWithoutExt := strings.TrimSuffix(decodedName, filepath.Ext(decodedName))
	finalName := nameWithoutExt + ".mp4"

	return sanitizeFilename(finalName)
}

// sanitizeFilename 移除 Windows 文件名非法字符
func sanitizeFilename(name string) string {
	// Windows 非法字符: < > : " / \ | ? *
	re := regexp.MustCompile(`[<>:"/\\|?*]`)
	return re.ReplaceAllString(name, "_")
}

// removeLinkFromFile 线程安全地从文件和内存切片中移除链接
func removeLinkFromFile(targetLink string) {
	fileMutex.Lock()
	defer fileMutex.Unlock()

	// 1. 从内存切片中移除
	newLinks := []string{}
	for _, l := range allLinks {
		if l != targetLink {
			newLinks = append(newLinks, l)
		}
	}
	allLinks = newLinks

	// 2. 重写文件
	f, err := os.Create(linkFile)
	if err != nil {
		fmt.Printf("警告: 无法更新链接文件: %v\n", err)
		return
	}
	defer f.Close()

	w := bufio.NewWriter(f)
	for _, l := range allLinks {
		fmt.Fprintln(w, l)
	}
	w.Flush()
}

// moveFile 处理跨设备移动 (尝试重命名，失败则复制+删除)
func moveFile(sourcePath, destPath string) error {
	// 1. 检查源文件是否存在
	if _, err := os.Stat(sourcePath); os.IsNotExist(err) {
		return fmt.Errorf("源文件不存在 (可能下载未生成): %s", sourcePath)
	}

	// 2. 尝试直接重命名 (同盘符极快)
	err := os.Rename(sourcePath, destPath)
	if err == nil {
		return nil
	}

	// 3. 如果重命名失败 (跨盘符/跨挂载点)，执行复制+删除
	inputFile, err := os.Open(sourcePath)
	if err != nil {
		return fmt.Errorf("无法打开源文件: %v", err)
	}
	// 这里不使用 defer Close，为了在删除前能显式关闭

	outputFile, err := os.Create(destPath)
	if err != nil {
		inputFile.Close()
		return fmt.Errorf("无法创建目标文件: %v", err)
	}
	
	_, err = io.Copy(outputFile, inputFile)
	if err != nil {
		inputFile.Close()
		outputFile.Close()
		return fmt.Errorf("复制文件失败: %v", err)
	}

	// 显式关闭文件流
	inputFile.Close()
	outputFile.Close()
	
	// 4. 删除源文件
	err = os.Remove(sourcePath)
	if err != nil {
		return fmt.Errorf("源文件删除失败 (但在目标处已存在): %v", err)
	}

	return nil
}

// getLinks 获取链接逻辑
func getLinks() []string {
	var links []string
	if _, err := os.Stat(linkFile); err == nil {
		file, err := os.Open(linkFile)
		if err == nil {
			scanner := bufio.NewScanner(file)
			for scanner.Scan() {
				line := strings.TrimSpace(scanner.Text())
				if line != "" {
					links = append(links, line)
				}
			}
			file.Close()
		}
	}

	if len(links) == 0 {
		fmt.Println("未检测到 'aria2_links.txt' 或文件为空。")
		fmt.Println("请输入 m3u8 链接 (每行一条，输入 'run' 开始下载):")
		scanner := bufio.NewScanner(os.Stdin)
		for scanner.Scan() {
			text := strings.TrimSpace(scanner.Text())
			if strings.ToLower(text) == "run" {
				break
			}
			if text != "" {
				links = append(links, text)
			}
		}
	}
	return links
}

// getUserConfig 获取用户交互输入
func getUserConfig() Config {
	reader := bufio.NewReader(os.Stdin)
	cfg := Config{}

	fmt.Print("请输入 yt-dlp 线程数 (例如 8): ")
	fmt.Scanln(&cfg.YtDlpThreads)
	if cfg.YtDlpThreads == "" { cfg.YtDlpThreads = "4" }

	fmt.Print("请输入同时执行的任务数 (例如 3): ")
	fmt.Scanln(&cfg.MaxConcurrent)
	if cfg.MaxConcurrent == 0 { cfg.MaxConcurrent = 1 }

	fmt.Print("请输入临时文件夹路径 (Temp): ")
	tempDir, _ := reader.ReadString('\n')
	cfg.TempDir = strings.TrimSpace(tempDir)
	if cfg.TempDir == "" { cfg.TempDir = "./temp_downloads" }

	fmt.Print("请输入目标文件夹路径 (Rclone Mount): ")
	targetDir, _ := reader.ReadString('\n')
	cfg.TargetDir = strings.TrimSpace(targetDir)
	if cfg.TargetDir == "" { cfg.TargetDir = "./completed" }

	return cfg
}

func ensureDir(dirName string) {
	if _, err := os.Stat(dirName); os.IsNotExist(err) {
		os.MkdirAll(dirName, 0755)
	}
}

func printHeader() {
	fmt.Println("========================================")
	fmt.Println("   M3U8 批量下载助手 (Go重构版 v2)      ")
	fmt.Println("========================================")
}
