# zutNet

中原工学院校园网的快速连接方式，基于Python

## 使用步骤

0.修改配置文件中的信息  
1.连接校园网  
2.运行Python或exe文件（使用时请不要代理1.1.1.1）  
3.登录成功  

## 编译为exe

安装pyinstaller

```sh
pip install pyinstaller
```

在当前目录下运行

```sh
pyinstaller net.py
```

点击 dist\net 文件夹中的 net.exe 文件即可运行。 **首次运行会自动生成配置文件，请编辑后再次运行。** 这会默认使用onedir打包，我建议对于这个脚本不要使用onefile打包，可能出现问题。

## 说明

此文件在原仓库的基础上使用AI进行了修改，使之通过登录页URL获取本机IP，不用再手动输入IP，并应该解决了多网卡和编译为exe情况下的问题，若仍有问题，请提交issues
