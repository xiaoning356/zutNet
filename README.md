# zutNet

中原工学院校园网的快速连接方式，基于Python

## 使用步骤

0.修改配置文件中的信息  
1.连接校园网  
2.运行Python文件（使用时请关闭代理）  
3.登录成功  

## 编译为exe

安装pyinstaller

```sh
pip install pyinstaller
```

在当前目录下运行

```sh
pyinstaller --onefile login_script.py
```

点击 dist 文件夹中的 login_script.exe 文件即可运行

## 说明

此文件在原仓库的基础上使用AI进行了修改，使之通过登录页URL获取本机IP，不用再手动输入IP，并应该解决了多网卡和编译为exe情况下的问题，若仍有问题，请提交issues
